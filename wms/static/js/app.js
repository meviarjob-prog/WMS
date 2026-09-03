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
 * Обработчик ввода со сканера штрихкода: сканер эмулирует быструю печать + Enter.
 * onScan(value) вызывается по Enter или после паузы в наборе.
 */
function initBarcodeInput(input, onScan) {
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const value = input.value.trim();
      if (value) {
        onScan(value);
        input.value = "";
      }
    }
  });
  input.addEventListener("focus", () => input.select());
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-autocomplete-nomenclature]").forEach(initNomenclatureAutocomplete);

  document.querySelectorAll("[data-autoprint]").forEach((el) => {
    if (el.dataset.autoprint === "1") {
      window.print();
    }
  });
});
