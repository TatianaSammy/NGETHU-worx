// QAD Enterprise Service Worker — Offline-First with Background Sync
// ─────────────────────────────────────────────────────────────────
//  • Static assets:        Stale-while-revalidate (instant shell loads)
//  • Navigation (GET):     Network-first, falls back to cached page snapshot
//  • Mutating requests:    Network-first, queued to IndexedDB on failure
//  • Background Sync:      Replays queued requests when connectivity restores

const CACHE_NAME = "qad-enterprise-pwa-v8";
const PAGE_CACHE = "qad-page-cache-v2";
const VOICE_CACHE = "qad-voice-alerts-v1";

const PRECACHE_ASSETS = [
    "/static/vendor/bootstrap/bootstrap.min.css",
    "/static/vendor/bootstrap/bootstrap.bundle.min.js",
    "/static/css/font_awesome/all.min.css",
    "/static/core/css/theme.css",
    "/static/core/css/base.css",
    "/static/core/css/header.css",
    "/static/core/css/pwa.css",
    "/static/core/js/theme.js",
    "/static/core/js/header.js",
    "/static/core/js/pwa.js",
    "/static/core/js/offline-queue.js",
    "/static/image/apple-touch-icon.png",
    "/manifest.json"
];

// Paths that should never be page-cached
const NO_PAGE_CACHE = [
    "/accounts/login/",
    "/accounts/logout/",
    "/health/",
    "/health/live/",
    "/health/ready/",
    "/accounts/session-check/"
];

const SYNC_TAG = "qad-offline-sync";

// ═══════════════════════════════════════════════════════════════
// INSTALL
// ═══════════════════════════════════════════════════════════════
self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(PRECACHE_ASSETS).catch((err) => {
                console.warn("[SW] Pre-cache partial fail:", err);
            });
        }).then(() => self.skipWaiting())
    );
});

// ═══════════════════════════════════════════════════════════════
// ACTIVATE — Clean old caches
// ═══════════════════════════════════════════════════════════════
self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys
                    .filter((key) => key !== CACHE_NAME && key !== PAGE_CACHE && key !== VOICE_CACHE && (
                        key.startsWith("qad-") ||
                        key.startsWith("qad-public-static-") ||
                        key.startsWith("qad-platform-")
                    ))
                    .map((key) => caches.delete(key))
            )
        ).then(() => self.clients.claim())
    );
});

// ═══════════════════════════════════════════════════════════════
// FETCH — Strategy router
// ═══════════════════════════════════════════════════════════════
self.addEventListener("fetch", (event) => {
    const { request } = event;
    const url = new URL(request.url);

    // Cross-origin requests bypass SW entirely
    if (url.origin !== self.location.origin) return;

    // ─── Mutating Requests (POST/PUT/PATCH/DELETE) ───────────
    if (request.method !== "GET") {
        event.respondWith(handleMutatingRequest(request));
        return;
    }

    // ─── Navigation (HTML pages) ─────────────────────────────
    if (request.mode === "navigate") {
        event.respondWith(handleNavigation(request, url));
        return;
    }

    if (url.pathname.includes("/voice_alerts/notifications/")) {
        event.respondWith((async () => {
            const cache = await caches.open(VOICE_CACHE);
            const cacheRequest = new Request(url.href, { credentials: "same-origin" });
            const cached = await cache.match(cacheRequest);
            if (cached) return cached;
            const response = await fetch(request);
            if (response.status === 206) {
                try {
                    const completeResponse = await fetch(cacheRequest);
                    if (completeResponse.ok && completeResponse.type === "basic") {
                        await cache.put(cacheRequest, completeResponse.clone());
                    }
                } catch (_) {}
            } else if (response.ok && response.type === "basic") {
                try {
                    await cache.put(cacheRequest, response.clone());
                } catch (_) {}
            }
            return response;
        })());
        return;
    }

    // ─── Static Assets: Stale-While-Revalidate ───────────────
    if (url.pathname.startsWith("/static/")) {
        event.respondWith(
            caches.open(CACHE_NAME).then(async (cache) => {
                const cachedResponse = await cache.match(request);
                const fetchPromise = fetch(request).then((networkResponse) => {
                    if (networkResponse.ok && networkResponse.type === "basic") {
                        cache.put(request, networkResponse.clone());
                    }
                    return networkResponse;
                }).catch(() => null);

                return cachedResponse || fetchPromise;
            })
        );
        return;
    }

    // ─── API/dynamic GETs: Network-first, no cache ───────────
    return;
});

// ═══════════════════════════════════════════════════════════════
// Navigation Handler — Network-first with offline fallback
// ═══════════════════════════════════════════════════════════════
async function handleNavigation(request, url) {
    const shouldCache = !NO_PAGE_CACHE.some(p => url.pathname === p || url.pathname.startsWith(p));

    try {
        const response = await fetch(new Request(request, { cache: "no-store" }));

        // Cache the page snapshot for offline fallback (only authenticated pages)
        if (response.ok && shouldCache) {
            const cache = await caches.open(PAGE_CACHE);
            cache.put(request, response.clone());
        }

        return response;
    } catch (err) {
        // Network failed — try to serve cached page snapshot
        const cache = await caches.open(PAGE_CACHE);
        const cached = await cache.match(request);

        if (cached) {
            // Inject an offline banner into the cached page
            const body = await cached.text();
            const offlineBanner = `
<div id="sw-offline-banner" style="
    position:fixed; top:0; left:0; right:0; z-index:2147483647;
    background:linear-gradient(135deg,#d97706,#b45309);
    color:#fff; text-align:center;
    padding:10px 16px; font:600 0.85rem/1.4 system-ui,sans-serif;
    box-shadow:0 4px 16px rgba(0,0,0,0.3);
    display:flex; align-items:center; justify-content:center; gap:10px;
">
    <span>⚡ You are offline — viewing cached data. Changes will be saved locally and synced when reconnected.</span>
    <button onclick="location.reload()" style="
        border:none; background:rgba(255,255,255,0.2); color:#fff;
        padding:4px 12px; border-radius:4px; cursor:pointer; font-weight:600; font-size:0.8rem;
    ">Retry</button>
</div>
<script>
window.addEventListener('online', function() {
    var b = document.getElementById('sw-offline-banner');
    if (b) { b.style.background = '#059669'; b.querySelector('span').textContent = '✓ Connection restored — syncing…'; setTimeout(function(){ b.remove(); }, 3000); }
});
</script>`;

            const modifiedBody = body.replace("</body>", offlineBanner + "\n</body>");

            return new Response(modifiedBody, {
                status: 200,
                headers: {
                    "Content-Type": "text/html; charset=utf-8",
                    "X-QAD-Offline": "cached-snapshot"
                }
            });
        }

        // No cached page — show the offline card
        return generateOfflinePage();
    }
}

// ═══════════════════════════════════════════════════════════════
// Mutating Request Handler — Queue on failure
// ═══════════════════════════════════════════════════════════════
async function handleMutatingRequest(request) {
    try {
        const response = await fetch(request.clone());
        return response;
    } catch (err) {
        // Network error — store in IndexedDB and return synthetic success
        console.warn("[SW] Mutating request failed, queueing:", request.url);

        try {
            const body = await request.clone().text();
            const headers = {};
            request.headers.forEach((value, key) => {
                headers[key] = value;
            });

            const entry = {
                url: request.url,
                method: request.method,
                headers: headers,
                body: body,
                contentType: headers["content-type"] || "application/x-www-form-urlencoded",
                timestamp: Date.now(),
                formOrigin: request.referrer || ""
            };

            await idbAdd(entry);

            // Register background sync
            if (self.registration && self.registration.sync) {
                await self.registration.sync.register(SYNC_TAG).catch(() => {});
            }

            // Notify all clients to update their UI
            const clients = await self.clients.matchAll({ type: "window" });
            clients.forEach(client => {
                client.postMessage({ type: "QUEUE_UPDATED" });
            });

            // If the user submitted a traditional HTML form via page navigation, return friendly HTML page
            if (request.mode === "navigate" || (request.headers.get("accept") && request.headers.get("accept").includes("text/html"))) {
                return generateOfflineSubmissionPage(request.referrer || "/");
            }

            return new Response(JSON.stringify({
                success: true,
                offline_queued: true,
                message: "Saved offline. Will sync automatically when connection restores."
            }), {
                status: 200,
                headers: { "Content-Type": "application/json" }
            });
        } catch (queueErr) {
            console.error("[SW] Failed to queue request:", queueErr);
            if (request.mode === "navigate" || (request.headers.get("accept") && request.headers.get("accept").includes("text/html"))) {
                return generateOfflinePage();
            }
            return new Response(JSON.stringify({
                success: false,
                message: "Network error and unable to save offline."
            }), {
                status: 503,
                headers: { "Content-Type": "application/json" }
            });
        }
    }
}

// ═══════════════════════════════════════════════════════════════
// Background Sync — Replay queued requests
// ═══════════════════════════════════════════════════════════════
self.addEventListener("sync", (event) => {
    if (event.tag === SYNC_TAG) {
        event.waitUntil(replayQueuedRequests());
    }
});

async function replayQueuedRequests() {
    const items = await idbGetAll();
    if (!items.length) return;

    console.info("[SW Sync] Replaying", items.length, "queued request(s)…");

    for (const item of items) {
        try {
            const headers = new Headers(item.headers || {});

            const response = await fetch(item.url, {
                method: item.method,
                headers: headers,
                body: item.body || undefined,
                credentials: "same-origin"
            });

            if (response.ok || (response.status >= 300 && response.status < 400) || (response.status >= 400 && response.status < 500)) {
                // Success or client error — remove from queue
                await idbDelete(item.id);
                console.info("[SW Sync] Replayed:", item.method, item.url, "→", response.status);
            } else {
                // Server error — keep for retry
                console.warn("[SW Sync] Server error, keeping in queue:", item.url, response.status);
            }
        } catch (err) {
            // Still offline — stop processing
            console.warn("[SW Sync] Still offline, will retry later:", err.message);
            break;
        }
    }

    // Notify clients
    const clients = await self.clients.matchAll({ type: "window" });
    clients.forEach(client => {
        client.postMessage({ type: "REPLAY_COMPLETE" });
    });
}

// ═══════════════════════════════════════════════════════════════
// IndexedDB Operations (inside Service Worker)
// ═══════════════════════════════════════════════════════════════
const IDB_NAME = "qad_offline_store";
const IDB_STORE = "pending_requests";

function idbOpen() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(IDB_NAME, 1);
        req.onupgradeneeded = (e) => {
            const db = e.target.result;
            if (!db.objectStoreNames.contains(IDB_STORE)) {
                const store = db.createObjectStore(IDB_STORE, { keyPath: "id", autoIncrement: true });
                store.createIndex("timestamp", "timestamp", { unique: false });
                store.createIndex("url", "url", { unique: false });
            }
        };
        req.onsuccess = (e) => resolve(e.target.result);
        req.onerror = (e) => reject(e.target.error);
    });
}

async function idbAdd(entry) {
    const db = await idbOpen();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, "readwrite");
        const store = tx.objectStore(IDB_STORE);
        const req = store.add(entry);
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

async function idbGetAll() {
    const db = await idbOpen();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, "readonly");
        const store = tx.objectStore(IDB_STORE);
        const req = store.getAll();
        req.onsuccess = () => resolve(req.result || []);
        req.onerror = () => reject(req.error);
    });
}

async function idbDelete(id) {
    const db = await idbOpen();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, "readwrite");
        const store = tx.objectStore(IDB_STORE);
        const req = store.delete(id);
        req.onsuccess = () => resolve();
        req.onerror = () => reject(req.error);
    });
}

// ═══════════════════════════════════════════════════════════════
// Offline Page Generator
// ═══════════════════════════════════════════════════════════════
function generateOfflinePage() {
    return new Response(
        `<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
    <meta name="theme-color" content="#0f172a">
    <title>Offline | NCWSCwaterLAB</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{min-height:100vh;display:grid;place-items:center;background:#0b1220;color:#e2e8f0;font-family:system-ui,-apple-system,sans-serif;text-align:center;padding:1.5rem}
        .offline-card{max-width:30rem;background:#1e293b;border:1px solid #334155;border-radius:1rem;padding:2.5rem 2rem;box-shadow:0 20px 25px -5px rgba(0,0,0,0.5)}
        .icon{font-size:3rem;margin-bottom:1rem}
        h1{margin:0 0 0.5rem;font-size:1.5rem;color:#f8fafc}
        p{margin:0 0 1.5rem;color:#94a3b8;font-size:0.95rem;line-height:1.5}
        .btn{display:inline-block;padding:0.75rem 1.5rem;border:0;border-radius:0.5rem;background:#0284c7;color:#fff;font-weight:600;font-size:0.95rem;cursor:pointer;transition:background 0.2s}
        .btn:hover{background:#0369a1}
        .queue-info{margin-top:1.5rem;padding:1rem;background:#0f172a;border:1px solid #1e3a5f;border-radius:0.75rem}
        .queue-info h3{font-size:0.85rem;color:#38bdf8;margin-bottom:0.35rem}
        .queue-info p{margin:0;font-size:0.8rem;color:#64748b}
        .pulse{animation:pulse 2s infinite}
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.5}}
    </style>
</head>
<body>
    <div class="offline-card">
        <div class="icon">⚡</div>
        <h1>Connection Offline</h1>
        <p>Don't worry — any data you've entered has been saved locally. It will automatically sync to the server when your connection is restored.</p>
        <button class="btn" onclick="location.reload()">Retry Connection</button>
        <div class="queue-info" id="queueInfo" style="display:none">
            <h3><span class="pulse">●</span> Pending Offline Data</h3>
            <p id="queueCount">Checking…</p>
        </div>
    </div>
    <script>
        if ('indexedDB' in self) {
            var req = indexedDB.open('qad_offline_store', 1);
            req.onsuccess = function(e) {
                try {
                    var db = e.target.result;
                    var tx = db.transaction('pending_requests', 'readonly');
                    var countReq = tx.objectStore('pending_requests').count();
                    countReq.onsuccess = function() {
                        var c = countReq.result;
                        if (c > 0) {
                            document.getElementById('queueInfo').style.display = 'block';
                            document.getElementById('queueCount').textContent = c + ' record(s) saved locally, waiting to sync.';
                        }
                    };
                } catch(ex) {}
            };
        }
        window.addEventListener('online', function() { location.reload(); });
    </script>
</body>
</html>`,
        {
            status: 503,
            headers: {
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "no-store"
            }
        }
    );
}

// ═══════════════════════════════════════════════════════════════
// Offline Submission Confirmation Page (for HTML forms)
// ═══════════════════════════════════════════════════════════════
function generateOfflineSubmissionPage(returnUrl) {
    var safeReturnUrl = returnUrl || "/";
    return new Response(
        `<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
    <meta name="theme-color" content="#0f172a">
    <title>Saved Offline | NCWSCwaterLAB</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{min-height:100vh;display:grid;place-items:center;background:#0b1220;color:#e2e8f0;font-family:system-ui,-apple-system,sans-serif;text-align:center;padding:1.5rem}
        .offline-card{max-width:32rem;background:#1e293b;border:1px solid #334155;border-radius:1rem;padding:2.5rem 2rem;box-shadow:0 20px 25px -5px rgba(0,0,0,0.5)}
        .icon{font-size:3rem;margin-bottom:1rem;color:#10b981}
        h1{margin:0 0 0.5rem;font-size:1.5rem;color:#f8fafc}
        p{margin:0 0 1.5rem;color:#94a3b8;font-size:0.95rem;line-height:1.5}
        .btn{display:inline-block;padding:0.75rem 1.5rem;border:0;border-radius:0.5rem;background:#0284c7;color:#fff;font-weight:600;font-size:0.95rem;text-decoration:none;cursor:pointer;transition:background 0.2s}
        .btn:hover{background:#0369a1}
        .badge{display:inline-block;background:#065f46;color:#a7f3d0;padding:0.25rem 0.75rem;border-radius:9999px;font-size:0.8rem;font-weight:600;margin-bottom:1rem}
    </style>
</head>
<body>
    <div class="offline-card">
        <div class="icon">✓</div>
        <div class="badge">Stored in Local Device Storage</div>
        <h1>Submission Saved Locally</h1>
        <p>You are currently offline. Your data has been recorded locally on this device and will automatically sync to the NCWSC water quality database as soon as network connection is restored.</p>
        <a href="${safeReturnUrl}" class="btn">Return to Application</a>
    </div>
    <script>
        window.addEventListener('online', function() {
            setTimeout(function() { window.location.href = "${safeReturnUrl}"; }, 1200);
        });
    </script>
</body>
</html>`,
        {
            status: 200,
            headers: {
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "no-store"
            }
        }
    );
}

