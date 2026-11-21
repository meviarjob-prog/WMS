// МВБ: формы сохраняются без перезагрузки страницы. Форма отправляется
// фоном, а из ответа сервера (той же страницы или страницы, куда он
// перенаправил) подменяются только содержимое, меню и сообщения — позиция
// прокрутки сохраняется. Отключить для формы: data-no-ajax.
(function () {
  if (!window.fetch || !window.DOMParser) return;

  function runScripts(container) {
    // Скрипты из подмененного HTML сами не выполняются — пересоздаем их.
    // DOMContentLoaded уже прошел, поэтому такие обработчики вызываем сразу.
    const origAdd = document.addEventListener;
    document.addEventListener = function (type, fn, opts) {
      if (type === "DOMContentLoaded") { try { fn.call(document, new Event(type)); } catch (e) { console.error(e); } return; }
      return origAdd.call(document, type, fn, opts);
    };
    try {
      container.querySelectorAll("script").forEach((old) => {
        const s = document.createElement("script");
        for (const a of old.attributes) s.setAttribute(a.name, a.value);
        s.textContent = old.textContent;
        old.replaceWith(s);
      });
    } finally {
      document.addEventListener = origAdd;
    }
  }

  function swap(html, url, samePage) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const content = doc.querySelector(".app-content");
    if (!content) { window.location.href = url; return; }
    document.title = doc.title;
    const nav = doc.querySelector("nav.navbar");
    const curNav = document.querySelector("nav.navbar");
    if (nav && curNav) curNav.innerHTML = nav.innerHTML;
    const cur = document.querySelector(".app-content");
    cur.innerHTML = content.innerHTML;
    const scripts = doc.getElementById("mvbPageScripts");
    const curScripts = document.getElementById("mvbPageScripts");
    if (scripts && curScripts) curScripts.innerHTML = scripts.innerHTML;
    if (!samePage) { history.pushState({}, "", url); window.scrollTo(0, 0); }
    runScripts(cur);
    if (curScripts) runScripts(curScripts);
    const alert = cur.querySelector(".alert");
    if (alert && samePage) {
      const r = alert.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) {
        // Сообщение вне экрана — показываем его всплывающим.
        const t = alert.cloneNode(true);
        t.style.cssText = "position:fixed;top:12px;right:12px;z-index:2000;max-width:420px;box-shadow:0 4px 16px rgba(0,0,0,.2)";
        document.body.appendChild(t);
        setTimeout(() => t.remove(), 5000);
      }
    }
  }

  document.addEventListener("submit", async (e) => {
    const form = e.target;
    if (e.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    const submitter = e.submitter;
    const method = ((submitter && submitter.getAttribute("formmethod")) || form.getAttribute("method") || "get").toLowerCase();
    const target = (submitter && submitter.getAttribute("formtarget")) || form.getAttribute("target");
    if (method !== "post" || target || form.hasAttribute("data-no-ajax") || (submitter && submitter.hasAttribute("data-no-ajax"))) return;
    const action = (submitter && submitter.getAttribute("formaction")) || form.getAttribute("action") || window.location.href;
    if (new URL(action, window.location.href).origin !== window.location.origin) return;
    e.preventDefault();
    const body = new FormData(form, submitter || undefined);
    const buttons = [...form.querySelectorAll("button, input[type=submit]")];
    buttons.forEach((b) => (b.disabled = true));
    try {
      const resp = await fetch(action, {method: "POST", body, headers: {"X-Requested-With": "mvb-ajax"}, credentials: "same-origin"});
      const type = resp.headers.get("Content-Type") || "";
      if (!type.includes("text/html")) {
        // Файл (PDF/Excel) — отдаем его как скачивание; повторно форму не
        // отправляем, чтобы действие не выполнилось дважды.
        buttons.forEach((b) => (b.disabled = false));
        if (/json/.test(type)) { window.location.reload(); return; }
        const blob = await resp.blob();
        const disp = resp.headers.get("Content-Disposition") || "";
        const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(disp);
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = m ? decodeURIComponent(m[1] || m[2]) : "file";
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        return;
      }
      const html = await resp.text();
      const finalUrl = resp.url || window.location.href;
      const samePage = new URL(finalUrl).pathname === window.location.pathname;
      swap(html, samePage ? window.location.href : finalUrl, samePage);
    } catch (err) {
      buttons.forEach((b) => (b.disabled = false));
      alert("Нет связи с сервером — данные не сохранены. Попробуйте еще раз.");
    }
  });

  window.addEventListener("popstate", () => window.location.reload());
})();
