/* Network-only service worker. It preserves PWA lifecycle support without
   caching pages, assets, notifications, or requests. */
self.addEventListener("install", event => {
    event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", event => {
    event.waitUntil((async () => {
        const clients = await self.clients.matchAll({ includeUncontrolled: true, type: "window" });
        const cacheNames = await caches.keys();
        const removingLegacyCaches = cacheNames.some(name => name.startsWith("qad-"));
        await Promise.all(cacheNames
            .filter(name => name.startsWith("qad-"))
            .map(name => caches.delete(name)));

        let databaseDeletionBlocked = false;
        await new Promise(resolve => {
            if (!self.indexedDB) return resolve();
            const request = self.indexedDB.deleteDatabase("qad_offline_store");
            request.onsuccess = resolve;
            request.onerror = resolve;
            request.onblocked = () => {
                databaseDeletionBlocked = true;
                resolve();
            };
        });

        await self.clients.claim();
        if (removingLegacyCaches || databaseDeletionBlocked) {
            await Promise.all(clients.map(client => {
                if (typeof client.navigate !== "function") return Promise.resolve(null);
                return client.navigate(client.url).catch(() => null);
            }));
        }
    })());
});

self.addEventListener("fetch", event => {
    event.respondWith(fetch(event.request, { cache: "no-store" }));
});
