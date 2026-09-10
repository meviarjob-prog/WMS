/**
 * Автокомплит "поиск по вхождению" для полей номенклатуры.
 * Использование: <div class="autocomplete-box" data-autocomplete-nomenclature>
 *   <input type="text" class="form-control ac-input" autocomplete="off" placeholder="Начните вводить...">
 *   <input type="hidden" class="ac-value" name="nomenclature_id">
 *   <div class="autocomplete-list"></div>
 * </div>
 */
function initNomenclatureAutocomplete(root) {
  const input = root.querySelector(".ac-input");
  const hidden = root.querySelector(".ac-value");
  const list = root.querySelector(".autocomplete-list");
  let items = [];
  let activeIndex = -1;
  let debounceTimer = null;

  function closeList() {
    list.classList.remove("show");
    list.innerHTML = "";
    activeIndex = -1;
  }

  function renderList() {
    list.innerHTML = "";
    if (items.length === 0) {
      closeList();
      return;
    }
    items.forEach((item, idx) => {
      const div = document.createElement("div");
      div.className = "autocomplete-item" + (idx === activeIndex ? " active" : "");
      div.textContent = item.label;
      div.addEventListener("mousedown", (e) => {
        e.preventDefault();
        selectItem(item);
      });
      list.appendChild(div);
    });
    list.classList.add("show");
  }

  function selectItem(item) {
    input.value = item.name;
    hidden.value = item.id;
    input.dataset.selectedName = item.name;
    input.dataset.selectedUnit = item.unit || "";
    closeList();
    input.dispatchEvent(new CustomEvent("nomenclature-selected", { detail: item }));
  }

  input.addEventListener("input", () => {
    hidden.value = "";
    const q = input.value.trim();
    clearTimeout(debounceTimer);
    if (q.length < 1) {
      closeList();
      return;
    }
    debounceTimer = setTimeout(() => {
      fetch("/api/nomenclature/search?q=" + encodeURIComponent(q))
        .then((r) => r.json())
        .then((data) => {
          items = data;
          activeIndex = -1;
          renderList();
        });
    }, 200);
  });

  input.addEventListener("keydown", (e) => {
    if (!list.classList.contains("show")) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = Math.min(activeIndex + 1, items.length - 1);
      renderList();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = Math.max(activeIndex - 1, 0);
      renderList();
    } else if (e.key === "Enter") {
      if (activeIndex >= 0) {
        e.preventDefault();
        selectItem(items[activeIndex]);
      }
    } else if (e.key === "Escape") {
      closeList();
    }
  });

  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) closeList();
  });
}

/**
 * Обработчик ввода со сканера штрихкода. Срабатывает по Enter (если сканер
 * его присылает) — сразу, ИЛИ автоматически после короткой паузы в наборе
 * (если сканер настроен без Enter, либо просто не был нажат) — так товар
 * добавляется сканированием без необходимости нажимать Enter.
 * onScan(value) вызывается ровно один раз на скан.
 *
 * Защита от "слипания" двух сканирований в один мусорный номер: если короб
 * сканируют дважды подряд быстрее, чем срабатывает debounce (нервное
 * повторное сканирование, или сканер с двойным срабатыванием на одно
 * нажатие) — второй скан начинает печататься в то же поле, не дожидаясь,
 * пока первый успеет отправиться и очистить его. Символы физического
 * сканера идут практически без пауз (единицы мс) — заметно быстрее, чем
 * может выдать даже очень быстрый человек на клавиатуре. Поэтому "разрыв"
 * такого рода детектируем только внутри буфера, где ВСЕ символы шли строго
 * быстрее человеческого предела (FAST_CHAR_GAP_MS) — если хотя бы один
 * символ пришел медленнее, считаем ввод ручным и эту логику для всего
 * оставшегося буфера больше не применяем (копится как раньше, до
 * Enter/debounce) — так пауза человека посреди набора номера никогда не
 * стирает то, что он уже ввел.
 */
function initBarcodeInput(input, onScan, options) {
  const debounceMs = (options && options.debounceMs) || 350;
  const FAST_CHAR_GAP_MS = 25; // быстрее человека, но с запасом ниже скорости сканера
  const RESET_GAP_MS = 100; // пауза, которая обрывает "быструю" (сканерную) серию
  let timer = null;
  let lastKeyTime = 0;
  let bufferIsFastSoFar = true;

  function fire() {
    clearTimeout(timer);
    timer = null;
    const value = input.value.trim();
    if (value) {
      onScan(value);
      input.value = "";
    }
    bufferIsFastSoFar = true;
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      fire();
    }
  });

  input.addEventListener("input", (e) => {
    const now = Date.now();
    const gap = now - lastKeyTime;
    const isFreshField = input.value.length <= 1;

    if (isFreshField) {
      bufferIsFastSoFar = true;
    } else if (gap > FAST_CHAR_GAP_MS && gap <= RESET_GAP_MS) {
      // Пауза уже не "сканерная", но еще не настолько большая, чтобы
      // уверенно считать ее границей между двумя сканами (может быть и
      // просто чуть замешкавшийся человек) — просто перестаем угадывать
      // границы сканов для этого буфера, ничего не стираем.
      bufferIsFastSoFar = false;
    } else if (
      timer !== null &&
      bufferIsFastSoFar &&
      gap > RESET_GAP_MS &&
      input.value.length > 3 &&
      typeof e.data === "string" &&
      e.data
    ) {
      // До сих пор весь буфер набирался строго на скорости сканера (иначе
      // сработала бы ветка выше) — длинная пауза именно ПОСЛЕ такого
      // быстрого буфера означает конец одного скана и начало следующего,
      // а не паузу внутри ручного набора. Проверка длины (>3) — на всякий
      // случай, чтобы не стирать совсем короткий ввод, если он все же
      // окажется случайным.
      input.value = e.data;
      bufferIsFastSoFar = true;
    } else if (gap > FAST_CHAR_GAP_MS) {
      bufferIsFastSoFar = false;
    }

    lastKeyTime = now;
    clearTimeout(timer);
    timer = setTimeout(fire, debounceMs);
  });

  input.addEventListener("focus", () => input.select());
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-autocomplete-nomenclature]").forEach(initNomenclatureAutocomplete);

  // Поле формы, которое должно уходить в отправку по Enter — в т.ч. по
  // синтетическому Enter от сканирования камерой (scanner.js), который
  // браузер сам по себе (в отличие от настоящего нажатия клавиши) не
  // отправляет как обычную форму.
  document.querySelectorAll("[data-submit-on-enter]").forEach((input) => {
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        if (input.value.trim() && input.form) {
          input.form.submit();
        }
      }
    });
  });

  document.querySelectorAll("[data-autoprint]").forEach((el) => {
    if (el.dataset.autoprint === "1") {
      window.print();
    }
  });
});
