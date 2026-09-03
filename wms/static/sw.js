// Минимальный service worker — только для того, чтобы браузер (в первую
// очередь Chrome на Android) предлагал "Добавить на экран" / "Установить"
// как полноценное приложение. Данные WMS не кэшируются: все запросы всегда
// идут в сеть, офлайн-режим не поддерживается (складу нужны актуальные
// остатки, а не устаревший кэш).

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", () => {
  // намеренно ничего не делаем — запрос идет в сеть как обычно
});
