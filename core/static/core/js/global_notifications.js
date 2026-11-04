(function () {
    "use strict";

    const container = document.querySelector(".notification-container");
    const btn = document.getElementById("notificationBtn");
    const dropdown = document.getElementById("notificationDropdown");
    const badge = document.getElementById("notificationBadge");
    if (!container || !btn || !dropdown || !badge) return;

    const api = {
        alerts: [],
        refreshTimer: null,
        initialized: false,
        autoVoicePlayed: new Set(),
        pendingVoice: [],
        voiceSources: new Map(),
        voiceQueue: Promise.resolve(),
        voiceEnabled: false,
        offlineMode: !navigator.onLine,
        offlineRepeatTimer: null,
        userId: String(container.dataset.userId || "anonymous"),
        fetchUrl: container.dataset.feedUrl || "/accounts/notifications/",
        ackUrlTemplate: container.dataset.ackUrl || "/ca/notifications/__ID__/ack/",

        voicePreferenceKey() {
            return `qad-voice-enabled:${this.userId}`;
        },

        alertStorageKey() {
            return `qad-notifications:${this.userId}:${this.plantId() || "all"}`;
        },

        voiceRequestUrl(value) {
            const safe = this.safeUrl(value);
            if (!safe) return "";
            const url = new URL(safe);
            url.searchParams.set("qad_voice_user", this.userId);
            url.searchParams.set("qad_voice_plant", this.plantId() || "all");
            return url.href;
        },

        scopedUrl() {
            const url = new URL(this.fetchUrl, window.location.origin);
            const plantId = this.plantId();
            if (plantId) url.searchParams.set("plant", plantId);
            return url.toString();
        },

        plantId() {
            const queryPlant = new URLSearchParams(window.location.search).get("plant");
            return queryPlant || container.dataset.plantId || document.body.dataset.plantId || "";
        },

        csrfToken() {
            const part = document.cookie.split(";").map(value => value.trim()).find(value => value.startsWith("csrftoken="));
            return part ? decodeURIComponent(part.slice("csrftoken=".length)) : "";
        },

        escape(value) {
            return String(value == null ? "" : value).replace(/[&<>"']/g, char => ({
                "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
            })[char]);
        },

        safeUrl(value) {
            if (!value) return "";
            try {
                const url = new URL(value, window.location.origin);
                return url.origin === window.location.origin ? url.href : "";
            } catch (_) {
                return "";
            }
        },

        async fetchNotifications() {
            try {
                const response = await fetch(this.scopedUrl(), {
                    credentials: "same-origin",
                    cache: "no-store",
                    headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json", "Cache-Control": "no-cache" }
                });
                if (!response.ok) throw new Error("Notification feed returned HTTP " + response.status);
                const data = await response.json();
                if (!data || !Array.isArray(data.alerts)) return;
                this.alerts = data.alerts;
                this.renderDropdown(data.alerts, Number(data.unread_count || 0));
                this.updateBadge(Number(data.unread_count || 0));
                this.persistAlerts(data.alerts);
                data.alerts.forEach(alert => {
                    if (alert.voice_url) this.cacheVoiceClip(alert.voice_url);
                });
                this.pruneVoiceCache(data.alerts);
                const status = dropdown.querySelector(".notification-status");
                if (status) status.hidden = true;
            } catch (error) {
                console.warn("[Dashboard notifications] Could not refresh alerts:", error);
                const restored = this.restoreAlerts();
                const status = dropdown.querySelector(".notification-status");
                if (status) {
                    status.textContent = restored
                        ? "Offline · showing saved notifications and cached voice alerts."
                        : "Notifications are unavailable. Connect once to save alerts for offline use.";
                    status.hidden = false;
                }
                if (restored) this.prepareOfflineVoiceAlerts();
            }
        },

        persistAlerts(alerts) {
            try {
                localStorage.setItem(this.alertStorageKey(), JSON.stringify({
                    savedAt: Date.now(),
                    alerts: (alerts || []).slice(0, 25)
                }));
            } catch (_) {}
        },

        restoreAlerts() {
            try {
                const saved = JSON.parse(localStorage.getItem(this.alertStorageKey()) || "null");
                if (!saved || !Array.isArray(saved.alerts)) return false;
                this.alerts = saved.alerts.filter(alert => alert && alert.id);
                const unread = this.alerts.filter(alert => !alert.acknowledged).length;
                this.renderDropdown(this.alerts, unread);
                this.updateBadge(unread);
                return true;
            } catch (_) {
                return false;
            }
        },

        async cacheVoiceClip(value) {
            const requestUrl = this.voiceRequestUrl(value);
            if (!requestUrl || !window.caches) return;
            try {
                const cache = await caches.open("qad-voice-alerts-v1");
                let response = await cache.match(requestUrl);
                if (!response && navigator.onLine) {
                    response = await fetch(requestUrl, { credentials: "same-origin", cache: "no-store" });
                    if (response.ok) await cache.put(requestUrl, response.clone());
                }
                if (response && response.ok && !this.voiceSources.has(requestUrl)) {
                    this.voiceSources.set(requestUrl, URL.createObjectURL(await response.blob()));
                }
            } catch (error) {
                console.info("[VoiceAlert] Could not cache this audio clip:", error);
            }
        },

        async pruneVoiceCache(alerts) {
            if (!window.caches) return;
            try {
                const keep = new Set((alerts || []).slice(0, 25)
                    .filter(alert => alert.voice_url)
                    .map(alert => this.voiceRequestUrl(alert.voice_url)));
                const cache = await caches.open("qad-voice-alerts-v1");
                const requests = await cache.keys();
                for (const request of requests) {
                    const cachedUrl = new URL(request.url);
                    if (
                        cachedUrl.searchParams.get("qad_voice_user") === this.userId
                        && cachedUrl.searchParams.get("qad_voice_plant") === (this.plantId() || "all")
                        && !keep.has(cachedUrl.href)
                    ) {
                        await cache.delete(request);
                        const source = this.voiceSources.get(cachedUrl.href);
                        if (source) URL.revokeObjectURL(source);
                        this.voiceSources.delete(cachedUrl.href);
                    }
                }
            } catch (error) {
                console.info("[VoiceAlert] Could not prune old cached clips:", error);
            }
        },

        async prepareOfflineVoiceAlerts() {
            const pending = this.alerts.filter(alert =>
                !alert.acknowledged && alert.auto_voice && alert.voice_url
            );
            await Promise.all(pending.map(alert => this.cacheVoiceClip(alert.voice_url)));
            if (this.voiceEnabled) this.playOfflinePendingAlerts();
            this.scheduleOfflineVoiceRepeats();
        },

        async playOfflinePendingAlerts() {
            if (!this.voiceEnabled || !this.offlineMode) return;
            for (const alert of this.alerts) {
                if (alert.acknowledged || !alert.auto_voice || !alert.voice_url) continue;
                if (this.autoVoicePlayed.has(String(alert.id))) continue;
                this.autoVoicePlayed.add(String(alert.id));
                await this.queueVoice(alert.voice_url, alert.id);
            }
        },

        scheduleOfflineVoiceRepeats() {
            if (this.offlineRepeatTimer) return;
            this.offlineRepeatTimer = window.setInterval(() => {
                if (!this.offlineMode || !this.voiceEnabled) return;
                this.autoVoicePlayed.clear();
                this.playOfflinePendingAlerts();
            }, 60000);
        },

        enableVoiceAlerts() {
            this.voiceEnabled = true;
            try { localStorage.setItem(this.voicePreferenceKey(), "true"); } catch (_) {}
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (AudioContext) {
                try {
                    this.audioContext = this.audioContext || new AudioContext();
                    this.audioContext.resume();
                } catch (_) {}
            }
            this.playChime();
            this.renderDropdown(this.alerts, this.alerts.filter(alert => !alert.acknowledged).length);
            const queued = this.pendingVoice.splice(0);
            queued.forEach(item => this.queueVoice(item.url, item.id));
            if (this.offlineMode) {
                this.autoVoicePlayed.clear();
                this.prepareOfflineVoiceAlerts();
            }
        },

        updateBadge(count) {
            badge.textContent = count > 99 ? "99+" : String(count);
            badge.style.display = count > 0 ? "grid" : "none";
            btn.classList.remove("has-alert--expired", "has-alert--almost-expiring", "has-alert--average", "has-alert--new");
            const urgencyOrder = ["expired", "almost-expiring", "average", "new"];
            const highest = urgencyOrder.find(kind => this.alerts.some(alert => !alert.acknowledged && alert.urgency === kind));
            if (highest) btn.classList.add("has-alert--" + highest);
            btn.setAttribute("aria-label", count ? `Notifications, ${count} unread` : "Notifications");
        },

        renderDropdown(alerts, unreadCount) {
            const rows = (alerts || []).slice(0, 15).map(alert => {
                const urgency = ["expired", "almost-expiring", "average", "new"].includes(alert.urgency) ? alert.urgency : "average";
                const url = this.safeUrl(alert.url);
                const canAck = Boolean(alert.can_acknowledge) && !alert.acknowledged;
                const ack = canAck
                    ? `<button type="button" class="notification-ack" data-notification-ack="${this.escape(alert.id)}">Acknowledge</button>`
                    : "";
                const open = url
                    ? `<a class="notification-open" href="${this.escape(url)}">${alert.type === "ca_assignment_pending" ? "Assign analyst" : "Open"}</a>`
                    : "";
                const voiceUrl = alert.voice_url || "";
                const profile = alert.voice_profile
                    ? `<span class="notification-voice-profile">VoiceOver · ${this.escape(alert.voice_profile)}</span>`
                    : "";
                const voiceBtn = `<button type="button" class="notification-voice-btn" data-notification-voice="${this.escape(voiceUrl)}" title="Replay configured VoiceOver audio" ${voiceUrl ? "" : "disabled"} style="display:inline-flex;align-items:center;gap:4px;padding:3px 7px;border-radius:4px;background:#eef2ff;color:#4f46e5;font-size:11px;font-weight:600;border:1px solid #c7d2fe;cursor:pointer;"><i class="fa-solid fa-volume-high" aria-hidden="true"></i> ${voiceUrl ? "Voice" : "Audio pending"}</button>${profile}`;
                return `<li class="notification-item notification-item--${urgency}" role="none">
                    <div class="notification-item-top"><span class="notification-urgency">${urgency.replace("-", " ")}</span><time>${this.escape(alert.time_ago || "")}</time></div>
                    <strong class="notification-item-title">${this.escape(alert.title || "Notification")}</strong>
                    <span class="notification-item-message">${this.escape(alert.message || "")}</span>
                    <div class="notification-item-actions">${voiceBtn}${open}${ack}</div>
                </li>`;
            }).join("");
            const voiceLabel = this.voiceEnabled ? "Unlock / replay voice" : "Enable voice alerts";
            dropdown.innerHTML = `<li class="notification-header" role="presentation"><span><i class="fa-solid fa-bell" aria-hidden="true"></i> Notifications</span><span class="notification-unread-count">${unreadCount} unread</span><button type="button" data-enable-voice class="notification-voice-toggle" style="border:1px solid #8eb8d8;border-radius:999px;padding:5px 9px;background:#eaf6ff;color:#075985;font-size:11px;font-weight:700;cursor:pointer">${voiceLabel}</button></li>${rows || '<li class="notification-empty" role="presentation">You are all caught up.</li>'}<li class="notification-status" role="status" aria-live="polite" hidden></li>`;
        },

        async ack(alertId) {
            const url = new URL(
                this.ackUrlTemplate.replace("__ID__", encodeURIComponent(alertId)),
                window.location.origin
            );
            const plantId = this.plantId();
            if (plantId) url.searchParams.set("plant", plantId);
            try {
                const response = await fetch(url.toString(), {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "X-CSRFToken": this.csrfToken(),
                        "X-Requested-With": "XMLHttpRequest",
                        "Content-Type": "application/x-www-form-urlencoded"
                    },
                    body: "alert_id=" + encodeURIComponent(alertId)
                });
                const data = await response.json();
                if (!response.ok || !data.success) throw new Error(data.error || "Could not acknowledge notification.");
                const redirectUrl = this.safeUrl(data.redirect_url);
                if (redirectUrl) {
                    window.location.assign(redirectUrl);
                    return;
                }
                this.alerts = this.alerts.filter(alert => String(alert.id) !== String(alertId));
                const unread = this.alerts.filter(alert => !alert.acknowledged).length;
                this.renderDropdown(this.alerts, unread);
                this.updateBadge(unread);
                this.persistAlerts(this.alerts);
                this.pruneVoiceCache(this.alerts);
                const streamItem = document.getElementById("alarm-item-" + String(alertId).replace(/[^A-Za-z0-9_-]/g, ""));
                if (streamItem) streamItem.remove();
            } catch (error) {
                console.error("[Dashboard notifications] Acknowledge failed:", error);
                const status = dropdown.querySelector(".notification-status");
                if (status) {
                    status.textContent = "Could not acknowledge this notification. Please try again.";
                    status.hidden = false;
                }
                this.scheduleRefresh();
            }
        },

        pushNotification(alert) {
            if (!alert || !alert.id) return;
            const next = Object.assign({ urgency: "new", acknowledged: false, time_ago: "Just now" }, alert);
            this.alerts = [next].concat(this.alerts.filter(item => item.id !== next.id));
            const unread = this.alerts.filter(item => !item.acknowledged).length;
            this.renderDropdown(this.alerts, unread);
            this.updateBadge(unread);
            this.persistAlerts(this.alerts);
            if (next.voice_url) this.cacheVoiceClip(next.voice_url);
        },

        requestAutoVoice(alertId, url, repeat) {
            if (!url) return;
            const id = String(alertId);
            this.cacheVoiceClip(url);
            if (!this.voiceEnabled) {
                this.pendingVoice = this.pendingVoice.filter(item => item.id !== id);
                this.pendingVoice.push({ id, url });
                return;
            }
            if (repeat || !this.autoVoicePlayed.has(id)) {
                if (!repeat) this.autoVoicePlayed.add(id);
                this.queueVoice(url, id);
            }
        },

        queueVoice(url, alertId) {
            this.voiceQueue = this.voiceQueue
                .catch(() => false)
                .then(() => {
                    if (alertId && !this.alerts.some(alert => String(alert.id) === String(alertId) && !alert.acknowledged)) {
                        return false;
                    }
                    return this._playVoice(url);
                });
            return this.voiceQueue;
        },

        playChime() {
            try {
                const Audio = window.AudioContext || window.webkitAudioContext;
                if (!Audio) return;
                const context = new Audio();
                const oscillator = context.createOscillator();
                const gain = context.createGain();
                oscillator.frequency.value = 660;
                gain.gain.setValueAtTime(.035, context.currentTime);
                gain.gain.exponentialRampToValueAtTime(.001, context.currentTime + .22);
                oscillator.connect(gain);
                gain.connect(context.destination);
                oscillator.start();
                oscillator.stop(context.currentTime + .22);
            } catch (_) { /* Audio is an optional enhancement. */ }
        },

        connectWebSocket() {
            if (this._wsConnected || !window.WebSocket) return;
            this._wsConnected = false;
            this._wsRetryDelay = 1000;
            this._wsRetryTimer = null;
            this._wsStopped = false;

            const self = this;
            function connect() {
                if (self._wsStopped || document.hidden || !navigator.onLine) return;
                clearTimeout(self._wsRetryTimer);
                const scheme = location.protocol === "https:" ? "wss:" : "ws:";
                let ws;
                try {
                    ws = new WebSocket(`${scheme}//${location.host}/ws/alerts/`);
                } catch (_) {
                    scheduleReconnect();
                    return;
                }
                self._ws = ws;
                ws.onopen = () => {
                    self._wsConnected = true;
                    self._wsRetryDelay = 1000;
                };
                ws.onmessage = event => {
                    let data;
                    try { data = JSON.parse(event.data); } catch (_) { return; }
                    if (data.type === "connection.ready") {
                        self.scheduleRefresh();
                        return;
                    }
                    // Real-time alert push — update bell immediately
                    if (data.type === "alert" || data.type === "alert.message") {
                        const msg = data.message || "";
                        const title = data.title || "Notification";
                        const alertId = data.id || data.instance_ref || ("ws_" + Date.now());
                        const acknowledged = data.state === "acknowledged";
                        const alreadyVisible = self.alerts.some(item => item.id === alertId);
                        self.pushNotification({
                            id: alertId,
                            type: data.alert_type || "ws_alert",
                            title: title,
                            message: msg,
                            urgency: data.priority === "critical" ? "expired" : (data.priority === "high" ? "almost-expiring" : "new"),
                            time_ago: "Just now",
                            acknowledged: acknowledged,
                            voice_url: data.voice_url || null,
                            voice_profile: data.voice_profile || "",
                            auto_voice: data.auto_voice !== false,
                        });
                        if (!alreadyVisible && !acknowledged) self.playChime();
                        if (data.auto_voice !== false && !acknowledged) self.requestAutoVoice(alertId, data.voice_url, data.repeat_voice);
                        // Also schedule a feed refresh to sync DB state
                        self.scheduleRefresh();
                        return;
                    }
                    // Persistent voice alert pushed over WebSocket
                    if (data.type === "voice_alert") {
                        const audioUrl = data.audio_url || data.voice_url || null;
                        const title = data.title || data.message || "Voice Alert";
                        const voiceId = data.id || ("voice_" + Date.now());
                        const alreadyVisible = self.alerts.some(item => item.id === voiceId);
                        self.pushNotification({
                            id: voiceId,
                            type: "voice_alert",
                            title: title,
                            message: data.message || "",
                            urgency: "new",
                            time_ago: "Just now",
                            acknowledged: false,
                            voice_url: audioUrl,
                            voice_profile: data.voice_profile || "",
                            auto_voice: data.auto_voice !== false,
                        });
                        if (!alreadyVisible) self.playChime();
                        if (data.auto_voice !== false) self.requestAutoVoice(voiceId, audioUrl, data.repeat_voice);
                        self.scheduleRefresh();
                        return;
                    }
                    if (data.type === "alert_acknowledged" || data.type === "alert.acknowledged") {
                        const targetId = String(data.id || (data.alert_state_id ? "state_" + data.alert_state_id : ""));
                        if (targetId) {
                            self.alerts = self.alerts.filter(item => String(item.id) !== targetId && String(item.db_id) !== String(data.alert_state_id));
                            const unread = self.alerts.filter(item => !item.acknowledged).length;
                            self.renderDropdown(self.alerts, unread);
                            self.updateBadge(unread);
                            self.persistAlerts(self.alerts);
                            const streamItem = document.getElementById("alarm-item-" + targetId.replace(/[^A-Za-z0-9_-]/g, ""));
                            if (streamItem) streamItem.remove();
                        }
                        if (self._currentVoiceAudio) {
                            try { self._currentVoiceAudio.pause(); } catch (_) {}
                        }
                        return;
                    }
                };
                ws.onerror = () => { if (ws) ws.close(); };
                ws.onclose = () => {
                    self._ws = null;
                    self._wsConnected = false;
                    scheduleReconnect();
                };
            }
            function scheduleReconnect() {
                if (self._wsStopped || document.hidden || !navigator.onLine) return;
                clearTimeout(self._wsRetryTimer);
                self._wsRetryTimer = window.setTimeout(connect, self._wsRetryDelay);
                self._wsRetryDelay = Math.min(self._wsRetryDelay * 2, 30000);
            }
            document.addEventListener("visibilitychange", () => {
                if (document.hidden) {
                    if (self._ws) self._ws.close();
                    return;
                }
                if (!self._ws) connect();
            });
            window.addEventListener("online", () => { if (!self._ws) connect(); });
            window.addEventListener("beforeunload", () => {
                self._wsStopped = true;
                clearTimeout(self._wsRetryTimer);
                if (self._ws) self._ws.close();
            }, { once: true });
            connect();
        },

        _playVoice(value) {
            const requestUrl = this.voiceRequestUrl(value);
            if (!requestUrl) return Promise.resolve(false);
            const source = this.voiceSources.get(requestUrl) || requestUrl;
            const start = audioSource => new Promise((resolve, reject) => {
                const audio = new Audio(audioSource);
                audio.preload = "auto";
                this._currentVoiceAudio = audio;
                const timeout = window.setTimeout(() => resolve(true), 90000);
                const finish = value => {
                    clearTimeout(timeout);
                    resolve(value);
                };
                audio.addEventListener("ended", () => finish(true), { once: true });
                audio.addEventListener("pause", () => {
                    if (!audio.ended) finish(false);
                }, { once: true });
                audio.addEventListener("error", () => {
                    clearTimeout(timeout);
                    reject(new Error("VoiceOver audio could not be decoded."));
                }, { once: true });
                try {
                    const playback = audio.play();
                    if (playback && playback.catch) playback.catch(reject);
                } catch (error) {
                    clearTimeout(timeout);
                    reject(error);
                }
            });
            return start(source).then(played => played).catch(async error => {
                try {
                    if (window.caches) {
                        const cache = await caches.open("qad-voice-alerts-v1");
                        const response = await cache.match(requestUrl);
                        if (response) {
                            const objectUrl = URL.createObjectURL(await response.blob());
                            this.voiceSources.set(requestUrl, objectUrl);
                            return await start(objectUrl);
                        }
                    }
                } catch (cachedError) {
                    console.warn("[VoiceAlert] Cached VoiceOver clip could not play:", cachedError);
                }
                console.info("[VoiceAlert] Voice playback was blocked or the clip is not cached:", error);
                const status = dropdown.querySelector(".notification-status");
                if (status) {
                    status.textContent = error && error.name === "NotAllowedError"
                        ? "The browser blocked automatic audio. Select Unlock / replay voice, then replay the saved VoiceOver clip."
                        : "Voice audio is unavailable offline because this clip has not been saved on this device yet.";
                    status.hidden = false;
                }
                return false;
            });
        },

        scheduleRefresh() {
            clearTimeout(this.refreshTimer);
            this.refreshTimer = window.setTimeout(() => this.fetchNotifications(), 100);
        },

        init() {
            if (this.initialized) return;
            this.initialized = true;
            try { this.voiceEnabled = localStorage.getItem(this.voicePreferenceKey()) === "true"; } catch (_) {}
            // The feed may refresh in the background, but it never opens the
            // drawer or restores an old open state from browser storage.
            dropdown.hidden = true;
            dropdown.setAttribute("aria-hidden", "true");
            btn.setAttribute("aria-expanded", "false");
            this.fetchNotifications();
            this.connectWebSocket();
            window.addEventListener("offline", () => {
                this.offlineMode = true;
                if (!this.alerts.length) this.restoreAlerts();
                this.prepareOfflineVoiceAlerts();
            });
            window.addEventListener("online", () => {
                this.offlineMode = false;
                if (this.offlineRepeatTimer) {
                    clearInterval(this.offlineRepeatTimer);
                    this.offlineRepeatTimer = null;
                }
                this.scheduleRefresh();
            });
            btn.addEventListener("click", () => this.fetchNotifications());
            dropdown.addEventListener("click", event => {
                const voiceToggle = event.target.closest("[data-enable-voice]");
                if (voiceToggle) {
                    event.stopPropagation();
                    this.enableVoiceAlerts();
                    return;
                }
                const voiceBtn = event.target.closest("[data-notification-voice]");
                if (voiceBtn) {
                    event.stopPropagation();
                    const src = voiceBtn.dataset.notificationVoice;
                    if (!src) return;
                    const icon = voiceBtn.querySelector("i");
                    if (icon) icon.className = "fa-solid fa-spinner fa-spin";
                    const resetIcon = () => {
                        if (icon) icon.className = "fa-solid fa-volume-high";
                    };

                    const playback = this._playVoice(src);
                    if (this._currentVoiceAudio) {
                        this._currentVoiceAudio.addEventListener("ended", resetIcon, { once: true });
                        this._currentVoiceAudio.addEventListener("pause", resetIcon, { once: true });
                    }
                    playback.finally(resetIcon);
                    return;
                }
                const button = event.target.closest("[data-notification-ack]");
                if (button) this.ack(button.dataset.notificationAck);
            });
            window.addEventListener("focus", () => {
                if (!document.hidden) this.fetchNotifications();
            });
            window.addEventListener("qad:resume", () => this.fetchNotifications());
            window.addEventListener("qad:data-changed", () => this.scheduleRefresh());
        }
    };

    window.GlobalNotifications = api;
    window.CA_Notifications = api;
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => api.init(), { once: true });
    } else {
        api.init();
    }
})();
