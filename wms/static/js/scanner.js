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
let _cameraScanModalEl = null;
let _cameraScanModal = null;

function _getCameraScanModal() {
  if (!_cameraScanModal) {
    _cameraScanModalEl = document.getElementById("cameraScanModal");
    _cameraScanModal = new bootstrap.Modal(_cameraScanModalEl);
    _cameraScanModalEl.addEventListener("hidden.bs.modal", () => {
      _stopCameraScan();
    });
  }
  return _cameraScanModal;
}

function _stopCameraScan() {
  if (_cameraScanInstance) {
    const inst = _cameraScanInstance;
    _cameraScanInstance = null;
    inst
      .stop()
      .then(() => inst.clear())
      .catch(() => {});
  }
}

function isCameraScanSupported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.isSecureContext);
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

  statusEl.textContent = "Запуск камеры...";
  _getCameraScanModal().show();

  _cameraScanInstance = new Html5Qrcode("cameraScanReader");
  const config = { fps: 10, qrbox: { width: 260, height: 160 } };

  _cameraScanInstance
    .start(
      { facingMode: "environment" },
      config,
      (decodedText) => {
        statusEl.textContent = "Найдено: " + decodedText;
        _stopCameraScan();
        _getCameraScanModal().hide();
        onResult(decodedText);
      },
      () => {
        /* не удалось распознать текущий кадр — это нормально, продолжаем сканировать */
      }
    )
    .catch((err) => {
      statusEl.textContent = "Не удалось открыть камеру: " + err;
    });
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
