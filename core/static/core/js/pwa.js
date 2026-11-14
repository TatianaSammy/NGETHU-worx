/* NCWSCwaterLAB — High Performance PWA shell */
(function () {
    "use strict";

    // 1. Inject PWA stylesheet if missing
    if (document.head && !document.querySelector('link[data-pwa-styles]')) {
        var pwaStyles = document.createElement("link");
        pwaStyles.rel = "stylesheet";
        pwaStyles.href = "/static/core/css/pwa.css";
        pwaStyles.dataset.pwaStyles = "true";
        document.head.appendChild(pwaStyles);
    }

    // 1b. Inject Offline Queue engine if missing
    if (document.head && !document.querySelector('script[data-offline-queue]') && typeof window.QADOfflineQueue === "undefined") {
        var queueScript = document.createElement("script");
        queueScript.src = "/static/core/js/offline-queue.js";
        queueScript.dataset.offlineQueue = "true";
        document.head.appendChild(queueScript);
    }

    // 2. Service Worker Registration
    if ("serviceWorker" in navigator && (window.isSecureContext || window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1")) {
        window.addEventListener("load", function () {
            navigator.serviceWorker.register("/sw.js", {
                scope: "/",
                updateViaCache: "none"
            }).then(function (reg) {
                if (reg && typeof reg.update === "function") {
                    reg.update().catch(function () {});
                }
            }).catch(function (error) {
                console.warn("[QAD PWA] Service worker registration:", error);
            });
        }, { once: true });
    }

    // 3. Safe Session Navigation
    window.addEventListener("keydown", function (e) {
        if (e.key === "Escape" || e.keyCode === 27) {
            if (document.fullscreenElement) {
                document.exitFullscreen().catch(function () {});
            }
        }
    });

    // 4. Offline / Online Network Indicator
    var networkNotice = null;

    function updateNetworkNotice() {
        if (!document.body) return;
        if (!networkNotice) {
            networkNotice = document.createElement("div");
            networkNotice.className = "pwa-network-notice";
            networkNotice.setAttribute("role", "status");
            networkNotice.setAttribute("aria-live", "polite");
            networkNotice.hidden = true;
            document.body.appendChild(networkNotice);
        }
        if (!navigator.onLine) {
            networkNotice.textContent = "⚡ You are offline. Changes are saved locally on this device and will sync automatically when reconnected.";
            networkNotice.hidden = false;
        } else if (!networkNotice.hidden) {
            networkNotice.textContent = "✓ Connection restored. Syncing offline data to server…";
            window.setTimeout(function () {
                if (navigator.onLine && networkNotice) networkNotice.hidden = true;
            }, 3500);
        }
    }

    window.addEventListener("offline", updateNetworkNotice);
    window.addEventListener("online", updateNetworkNotice);

    // 5. Modern PWA Install Affordance
    var installPrompt = null;
    var installButton = null;

    function isAppleMobile() {
        return /iPhone|iPad|iPod/.test(navigator.userAgent) ||
            (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    }

    function isAlreadyInstalled() {
        return window.matchMedia("(display-mode: fullscreen)").matches ||
            window.matchMedia("(display-mode: standalone)").matches ||
            window.matchMedia("(display-mode: window-controls-overlay)").matches ||
            window.navigator.standalone === true;
    }

    function removeInstallButton() {
        if (installButton) {
            installButton.remove();
            installButton = null;
        }
    }

    function renderInstallButton() {
        if (isAlreadyInstalled() || (!installPrompt && !isAppleMobile()) || installButton || !document.body) return;

        installButton = document.createElement("button");
        installButton.id = "pwa-install-button";
        installButton.type = "button";
        installButton.setAttribute("aria-label", "Install NCWSCwaterLAB Application");
        installButton.innerHTML = isAppleMobile()
            ? '<i class="fa-solid fa-arrow-up-from-bracket" aria-hidden="true"></i><span>Add to Home</span>'
            : '<i class="fa-solid fa-cloud-arrow-down" aria-hidden="true"></i><span>Install App</span>';

        installButton.addEventListener("click", async function () {
            if (!installPrompt && isAppleMobile()) {
                window.alert("To install NCWSCwaterLAB on iOS:\n1. Tap the Share button in Safari.\n2. Tap 'Add to Home Screen'.");
                return;
            }
            if (!installPrompt) return;
            installButton.disabled = true;
            try {
                await installPrompt.prompt();
                await installPrompt.userChoice;
            } catch (err) {
                console.warn("[QAD PWA] Install prompt failed:", err);
            } finally {
                installPrompt = null;
                removeInstallButton();
            }
        });

        document.body.appendChild(installButton);
    }

    window.addEventListener("beforeinstallprompt", function (event) {
        event.preventDefault();
        installPrompt = event;
        renderInstallButton();
    });

    window.addEventListener("appinstalled", function () {
        installPrompt = null;
        removeInstallButton();
    });

    function cleanupLegacyRouteMasking() {
        if (window.location.hash && window.location.hash.startsWith("#/")) {
            try {
                var cleanUrl = window.location.pathname + window.location.search;
                window.history.replaceState(null, document.title, cleanUrl);
            } catch (e) {}
        }
        try {
            if (typeof sessionStorage !== "undefined") {
                for (var i = sessionStorage.length - 1; i >= 0; i--) {
                    var key = sessionStorage.key(i);
                    if (key && key.startsWith("qad_route_")) {
                        sessionStorage.removeItem(key);
                    }
                }
            }
        } catch (e) {}
    }

    // Run route cleanup immediately
    cleanupLegacyRouteMasking();

    function initializePwaShell() {
        cleanupLegacyRouteMasking();
        renderInstallButton();
        updateNetworkNotice();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initializePwaShell, { once: true });
    } else {
        initializePwaShell();
    }
})();
