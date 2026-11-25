/* Network-required PWA registration and connection status. */
(function () {
    "use strict";

    if (document.head && !document.querySelector('link[data-pwa-styles]')) {
        const stylesheet = document.createElement("link");
        stylesheet.rel = "stylesheet";
        stylesheet.href = "/static/core/css/pwa.css?v=20261115-network-only";
        stylesheet.dataset.pwaStyles = "true";
        document.head.appendChild(stylesheet);
    }

    let networkNotice;
    function clearLegacyOfflineData() {
        function clearStorage(storageName, prefix) {
            try {
                const storage = window[storageName];
                if (!storage) return;
                for (let cursor = storage.length - 1; cursor >= 0; cursor -= 1) {
                    const key = storage.key(cursor);
                    if (key && key.startsWith(prefix)) storage.removeItem(key);
                }
            } catch (error) {
                console.warn("[QAD PWA] Could not remove a legacy offline storage entry:", error);
            }
        }
        clearStorage("localStorage", "qad-notifications:");
        clearStorage("sessionStorage", "maintenance-incident-queue-v1:");
    }

    function updateNetworkNotice() {
        if (!document.body) return;
        if (!networkNotice) {
            networkNotice = document.createElement("div");
            networkNotice.className = "pwa-network-notice";
            networkNotice.setAttribute("role", "status");
            networkNotice.setAttribute("aria-live", "assertive");
            networkNotice.hidden = true;
            document.body.appendChild(networkNotice);
        }
        networkNotice.hidden = navigator.onLine;
        if (!navigator.onLine) {
            networkNotice.textContent = "Connection unavailable. This application requires a live network connection; unsent changes are not saved.";
        }
    }

    window.addEventListener("offline", updateNetworkNotice);
    window.addEventListener("online", updateNetworkNotice);

    if ("serviceWorker" in navigator && (
        window.isSecureContext
        || window.location.hostname === "localhost"
        || window.location.hostname === "127.0.0.1"
    )) {
        window.addEventListener("load", function () {
            navigator.serviceWorker.register("/sw.js", {
                scope: "/",
                updateViaCache: "none"
            }).then(function (registration) {
                return registration.update();
            }).catch(function (error) {
                console.warn("[QAD PWA] Network-only service worker registration failed:", error);
            });
        }, { once: true });
    }

    let installPrompt = null;
    let installButton = null;

    function isAppleMobile() {
        return /iPhone|iPad|iPod/.test(navigator.userAgent)
            || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    }

    function isAlreadyInstalled() {
        return window.matchMedia("(display-mode: standalone)").matches
            || window.matchMedia("(display-mode: minimal-ui)").matches
            || window.matchMedia("(display-mode: window-controls-overlay)").matches
            || window.navigator.standalone === true;
    }

    function removeInstallButton() {
        if (!installButton) return;
        installButton.remove();
        installButton = null;
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
            } catch (error) {
                console.warn("[QAD PWA] Install prompt failed:", error);
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

    function initializePwaShell() {
        clearLegacyOfflineData();
        updateNetworkNotice();
        renderInstallButton();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initializePwaShell, { once: true });
    } else {
        initializePwaShell();
    }
})();
