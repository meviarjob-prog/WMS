/**
 * Сканирование штрихкода камерой телефона (или ноутбука) прямо в браузере,
 * без физического сканера. Использует библиотеку html5-qrcode.
 *
 * Использование: кнопка с data-camera-scan-target="#idПоля" открывает камеру,
 * по успешному распознаванию подставляет значение в указанное поле и
 * эмулирует Enter/change — так же, как это делает обычный сканер-эмулятор
 * клавиатуры (initBarcodeInput в app.js).
 *
 * ВАЖНО: доступ к камере браузер разрешает только в защищенном контексте —
 * на localhost или по HTTPS. При заходе с телефона по Wi-Fi на IP-адрес
 * компьютера без HTTPS камера открываться не будет (сработает вручную ввод).
 */

let _cameraScanInstance = null;
let _cameraScanStarting = null; // обещание запуска — чтобы закрытие окна не оставляло «висящую» камеру
let _cameraScanModalEl = null;
let _cameraScanModal = null;
let _cameraScanSession = 0; // номер попытки: устаревшие ответы игнорируем
let _cameraScanOnResult = null;
let _cameraScanTimer = null;

const CAMERA_START_TIMEOUT_MS = 8000;

function _getCameraScanModal() {
  if (!_cameraScanModal) {
    _cameraScanModalEl = document.getElementById("cameraScanModal");
    _cameraScanModal = new bootstrap.Modal(_cameraScanModalEl);
    _cameraScanModalEl.addEventListener("hidden.bs.modal", () => {
      _cameraScanSession += 1;
      _clearCameraTimer();
      _stopCameraScan();
    });
  }
  return _cameraScanModal;
}

function _clearCameraTimer() {
  if (_cameraScanTimer) {
    clearTimeout(_cameraScanTimer);
    _cameraScanTimer = null;
  }
}

// Гасим все потоки камеры внутри окна сканера — запасной путь, если
// библиотека не успела/не смогла остановиться сама (из-за этого следующий
// запуск камеры потом зависал: она оставалась занятой прошлой попыткой).
function _releaseCameraTracks() {
  const reader = document.getElementById("cameraScanReader");
  if (!reader) return;
  reader.querySelectorAll("video").forEach((video) => {
    const stream = video.srcObject;
    if (stream && stream.getTracks) stream.getTracks().forEach((t) => t.stop());
    video.srcObject = null;
  });
}

function _stopCameraScan() {
  const inst = _cameraScanInstance;
  const starting = _cameraScanStarting;
  _cameraScanInstance = null;
  _cameraScanStarting = null;
  const finish = () => {
    _releaseCameraTracks();
    if (!inst) return;
    let stopped;
    try {
      stopped = inst.stop();
    } catch (e) {
      stopped = Promise.resolve();
    }
    Promise.resolve(stopped)
      .catch(() => {})
      .then(() => {
        try {
          inst.clear();
        } catch (e) {
          /* уже очищено */
        }
        _releaseCameraTracks();
      });
  };
  // Если камера еще запускается — дожидаемся результата запуска (успех или
  // ошибка) и только потом останавливаем, иначе stop() падает, а камера
  // остается включенной.
  // Но не вечно: если запуск «завис» и не отвечает, через 1,5 с гасим
  // потоки принудительно и идем дальше.
  if (starting) {
    Promise.race([
      starting.then(() => {}, () => {}),
      new Promise((resolve) => setTimeout(resolve, 1500)),
    ]).then(finish);
  } else {
    finish();
  }
}

function isCameraScanSupported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.isSecureContext);
}

function _setCameraStatus(html, withRetry) {
  const statusEl = document.getElementById("cameraScanStatus");
  statusEl.textContent = html;
  const old = document.getElementById("cameraScanRetry");
  if (old) old.remove();
  if (withRetry) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "cameraScanRetry";
    btn.className = "btn btn-sm btn-primary ms-2";
    btn.textContent = "Повторить";
    btn.addEventListener("click", () => _startCameraAttempt(0, true));
    statusEl.appendChild(btn);
  }
}

// Попытки запуска: 1) задняя камера, 2) первая камера из списка (на части
// телефонов facingMode не подходит и запуск «висит»), 3) последняя в списке.
async function _cameraSources(attempt) {
  if (attempt === 0) return { facingMode: "environment" };
  let cameras = [];
  try {
    // getCameras() сам просит доступ к камере и при зависшей камере тоже
    // не отвечает — ограничиваем ожидание.
    cameras = await Promise.race([
      Html5Qrcode.getCameras(),
      new Promise((resolve) => setTimeout(() => resolve([]), 3000)),
    ]);
  } catch (e) {
    cameras = [];
  }
  if (!cameras.length) return { facingMode: "user" };
  return attempt === 1 ? cameras[cameras.length - 1].id : cameras[0].id;
}

async function _startCameraAttempt(attempt, fresh) {
  const session = ++_cameraScanSession;
  _clearCameraTimer();
  if (fresh) {
    // полностью сбрасываем прошлую (возможно зависшую) попытку
    await new Promise((resolve) => {
      _stopCameraScan();
      setTimeout(resolve, 150);
    });
  }
  const statusEl = document.getElementById("cameraScanStatus");
  _setCameraStatus(attempt ? "Запуск камеры (попытка " + (attempt + 1) + ")..." : "Запуск камеры...");

  const source = await _cameraSources(attempt);
  if (session !== _cameraScanSession) return;

  const inst = new Html5Qrcode("cameraScanReader");
  _cameraScanInstance = inst;
  const config = { fps: 10, qrbox: { width: 260, height: 160 } };

  const starting = inst.start(
    source,
    config,
    (decodedText) => {
      if (session !== _cameraScanSession) return;
      statusEl.textContent = "Найдено: " + decodedText;
      const done = _cameraScanOnResult;
      _cameraScanSession += 1;
      _clearCameraTimer();
      _stopCameraScan();
      _getCameraScanModal().hide();
      if (done) done(decodedText);
    },
    () => {
      /* не удалось распознать текущий кадр — это нормально, продолжаем сканировать */
    }
  );
  _cameraScanStarting = starting;

  // Запуск «завис»: ни ответа, ни ошибки — пробуем другой способ, а после
  // трех попыток подсказываем, что проверить.
  _cameraScanTimer = setTimeout(() => {
    if (session !== _cameraScanSession) return;
    _cameraScanSession += 1;
    _stopCameraScan();
    if (attempt < 2) {
      setTimeout(() => _startCameraAttempt(attempt + 1, false), 200);
    } else {
      _setCameraStatus(
        "Камера не отвечает. Проверьте: разрешён ли доступ к камере для этого сайта (значок замка рядом с адресом), " +
          "не занята ли камера другим приложением, затем нажмите «Повторить».",
        true
      );
    }
  }, CAMERA_START_TIMEOUT_MS);

  starting
    .then(() => {
      if (session !== _cameraScanSession) return;
      _clearCameraTimer();
      _setCameraStatus("Наведите камеру на штрихкод");
    })
    .catch((err) => {
      if (session !== _cameraScanSession) return;
      _clearCameraTimer();
      _stopCameraScan();
      if (attempt < 2 && !/Permission|NotAllowed/i.test(String(err))) {
        setTimeout(() => _startCameraAttempt(attempt + 1, false), 200);
      } else {
        _setCameraStatus("Не удалось открыть камеру: " + err, true);
      }
    });
}

function openCameraScan(onResult) {
  const statusEl = document.getElementById("cameraScanStatus");

  if (typeof Html5Qrcode === "undefined") {
    statusEl.textContent = "Библиотека сканирования не загрузилась.";
    _getCameraScanModal().show();
    return;
  }

  if (!isCameraScanSupported()) {
    statusEl.textContent =
      "Камера недоступна: нужен HTTPS-адрес (или localhost). См. README — раздел про доступ с телефона.";
    _getCameraScanModal().show();
    return;
  }

  _cameraScanOnResult = onResult;
  const modal = _getCameraScanModal();
  _setCameraStatus("Запуск камеры...");
  // Запускаем камеру только когда окно полностью показано: пока оно
  // анимируется, контейнер видео имеет нулевой размер, и библиотека на части
  // устройств зависала на запуске.
  if (_cameraScanModalEl.classList.contains("show")) {
    _startCameraAttempt(0, true);
  } else {
    _cameraScanModalEl.addEventListener("shown.bs.modal", () => _startCameraAttempt(0, true), { once: true });
    modal.show();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-camera-scan-target]").forEach((btn) => {
    if (!isCameraScanSupported()) {
      btn.title = "Требуется HTTPS-адрес для доступа к камере";
    }
    btn.addEventListener("click", () => {
      const target = document.querySelector(btn.dataset.cameraScanTarget);
      if (!target) return;
      openCameraScan((text) => {
        target.value = text;
        target.focus();
        target.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        target.dispatchEvent(new Event("change", { bubbles: true }));
      });
    });
  });
});
