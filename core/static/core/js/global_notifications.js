(function () {
    "use strict";

    const container = document.querySelector(".notification-container");
    const btn = document.getElementById("notificationBtn");
    const dropdown = document.getElementById("notificationDropdown");
    const badge = document.getElementById("notificationBadge");
    if (!container || !btn || !dropdown || !badge) return;

    // The shared header is included by several dashboard shells. Avoid binding
    // duplicate click handlers if the asset is included more than once.
    if (window.GlobalNotificationsInitialized) return;
    window.GlobalNotificationsInitialized = true;

    const api = {
        alerts: [],
        refreshTimer: null,
        initialized: false,
        autoVoicePlayed: new Set(),
        pendingVoice: [],
        voiceQueue: Promise.resolve(),
        acknowledgementsInFlight: new Set(),
        feedLoaded: false,
        userId: String(container.dataset.userId || "anonymous"),
        fetchUrl: container.dataset.feedUrl || "/accounts/notifications/",
        ackUrlTemplate: container.dataset.ackUrl || "/ca/notifications/__ID__/ack/",

        voiceRequestUrl(value) {
            return this.safeUrl(value);
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
                const knownAlerts = new Map(this.alerts.map(alert => [String(alert.id), alert]));
                this.alerts = data.alerts;
                this.renderDropdown(data.alerts, Number(data.unread_count || 0));
                this.updateBadge(Number(data.unread_count || 0));
                data.alerts.forEach(alert => {
                    if (
                        alert.auto_voice
                        && (alert.voice_url || alert.message)
                        && !alert.acknowledged
                        && (
                            !this.feedLoaded
                            ||
                            !knownAlerts.has(String(alert.id))
                            || !knownAlerts.get(String(alert.id)).voice_url
                            || !knownAlerts.get(String(alert.id)).auto_voice
                        )
                    ) this.requestAutoVoice(alert.id, alert.voice_url, false, alert.message);
                });
                this.feedLoaded = true;
                const status = dropdown.querySelector(".notification-status");
                if (status) status.hidden = true;
            } catch (error) {
                console.warn("[Dashboard notifications] Could not refresh alerts:", error);
                const status = dropdown.querySelector(".notification-status");
                if (status) {
                    status.textContent = "Live notifications are unavailable. Reconnect to refresh the alert feed.";
                    status.hidden = false;
                }
            }
        },

        unlockVoicePlayback() {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (AudioContext) {
                try {
                    this.audioContext = this.audioContext || new AudioContext();
                    if (this.audioContext.state === "suspended") this.audioContext.resume();
                } catch (_) {}
            }
            if (window.speechSynthesis && window.speechSynthesis.paused) {
                try { window.speechSynthesis.resume(); } catch (_) {}
            }
            const queued = this.pendingVoice.splice(0);
            queued.forEach(item => this.queueVoice(item.url, item.id, item.text));
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
                    ? `<button type="button" class="notification-ack" data-notification-ack="${this.escape(alert.id)}" data-notification-open="${this.escape(url)}">Acknowledge</button>`
                    : "";
                const open = url && !canAck
                    ? `<a class="notification-open" href="${this.escape(url)}">${alert.type === "ca_assignment_pending" ? "Assign analyst" : "Open"}</a>`
                    : "";
                const voiceUrl = alert.voice_url || "";
                const profile = alert.voice_profile
                    ? `<span class="notification-voice-profile">VoiceOver · ${this.escape(alert.voice_profile)}</span>`
                    : "";
                const voiceAvailable = Boolean(voiceUrl || alert.message);
                const voiceBtn = `<button type="button" class="notification-voice-btn" data-notification-voice="${this.escape(voiceUrl)}" data-notification-voice-text="${this.escape(alert.message || "")}" title="Replay voice notification" ${voiceAvailable ? "" : "disabled"} style="display:inline-flex;align-items:center;gap:4px;padding:3px 7px;border-radius:4px;background:#eef2ff;color:#4f46e5;font-size:11px;font-weight:600;border:1px solid #c7d2fe;cursor:pointer;"><i class="fa-solid fa-volume-high" aria-hidden="true"></i> ${voiceAvailable ? "Voice" : "Audio unavailable"}</button>${profile}`;
                return `<li class="notification-item notification-item--${urgency}" role="none">
                    <div class="notification-item-top"><span class="notification-urgency">${urgency.replace("-", " ")}</span><time>${this.escape(alert.time_ago || "")}</time></div>
                    <strong class="notification-item-title">${this.escape(alert.title || "Notification")}</strong>
                    <span class="notification-item-message">${this.escape(alert.message || "")}</span>
                    <div class="notification-item-actions">${voiceBtn}${open}${ack}</div>
                </li>`;
            }).join("");
            dropdown.innerHTML = `<li class="notification-header" role="presentation"><span><i class="fa-solid fa-bell" aria-hidden="true"></i> Notifications</span><span class="notification-unread-count">${unreadCount} unread</span><span aria-label="Voice notifications enabled" style="font-size:11px;font-weight:700;color:#075985">Voice on</span></li>${rows || '<li class="notification-empty" role="presentation">You are all caught up.</li>'}<li class="notification-status" role="status" aria-live="polite" hidden></li>`;
        },

        async ack(alertId, button, destination = "") {
            const key = String(alertId);
            if (this.acknowledgementsInFlight.has(key)) return;
            this.acknowledgementsInFlight.add(key);
            if (button) {
                button.disabled = true;
                button.setAttribute("aria-busy", "true");
                button.textContent = "Acknowledging…";
            }
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
                const destinationUrl = this.safeUrl(data.redirect_url) || this.safeUrl(destination);
                if (window.speechSynthesis) window.speechSynthesis.cancel();
                if (destinationUrl) {
                    window.location.assign(destinationUrl);
                    return;
                }
                this.alerts = this.alerts.filter(alert => String(alert.id) !== String(alertId));
                const unread = this.alerts.filter(alert => !alert.acknowledged).length;
                this.renderDropdown(this.alerts, unread);
                this.updateBadge(unread);
                const streamItem = document.getElementById("alarm-item-" + String(alertId).replace(/[^A-Za-z0-9_-]/g, ""));
                if (streamItem) streamItem.remove();
            } catch (error) {
                console.error("[Dashboard notifications] Acknowledge failed:", error);
                const status = dropdown.querySelector(".notification-status");
                if (status) {
                    status.textContent = error.message || "Could not acknowledge this notification. Please try again.";
                    status.hidden = false;
                }
                if (button && button.isConnected) {
                    button.disabled = false;
                    button.removeAttribute("aria-busy");
                    button.textContent = "Acknowledge";
                }
                this.scheduleRefresh();
            } finally {
                this.acknowledgementsInFlight.delete(key);
            }
        },

        pushNotification(alert) {
            if (!alert || !alert.id) return;
            const next = Object.assign({ urgency: "new", acknowledged: false, time_ago: "Just now" }, alert);
            this.alerts = [next].concat(this.alerts.filter(item => item.id !== next.id));
            const unread = this.alerts.filter(item => !item.acknowledged).length;
            this.renderDropdown(this.alerts, unread);
            this.updateBadge(unread);
        },

        requestAutoVoice(alertId, url, repeat, text) {
            if (!url && !text) return;
            const id = String(alertId);
            const alreadyPending = this.pendingVoice.some(item => item.id === id);
            if (repeat || (!this.autoVoicePlayed.has(id) && !alreadyPending)) {
                if (!repeat) this.autoVoicePlayed.add(id);
                this.queueVoice(url, id, text);
            }
        },

        queueVoice(url, alertId, text) {
            this.voiceQueue = this.voiceQueue
                .catch(() => false)
                .then(() => {
                    if (alertId && !this.alerts.some(alert => String(alert.id) === String(alertId) && !alert.acknowledged)) {
                        return false;
                    }
                    return this._playVoice(url, text).then(played => {
                        if (!played && alertId && this.alerts.some(alert => String(alert.id) === String(alertId) && !alert.acknowledged)) {
                            this.autoVoicePlayed.delete(String(alertId));
                            if (!this.pendingVoice.some(item => item.id === String(alertId))) {
                                this.pendingVoice.push({ id: String(alertId), url, text });
                            }
                        }
                        return played;
                    });
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
                        if (data.auto_voice !== false && !acknowledged) self.requestAutoVoice(alertId, data.voice_url, data.repeat_voice, msg);
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
                        if (data.auto_voice !== false) self.requestAutoVoice(voiceId, audioUrl, data.repeat_voice, data.message || title);
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
                            const streamItem = document.getElementById("alarm-item-" + targetId.replace(/[^A-Za-z0-9_-]/g, ""));
                            if (streamItem) streamItem.remove();
                        }
                        if (self._currentVoiceAudio) {
                            try { self._currentVoiceAudio.pause(); } catch (_) {}
                        }
                        if (window.speechSynthesis) window.speechSynthesis.cancel();
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

        async _playVoice(value, fallbackText = "") {
            const requestUrl = this.voiceRequestUrl(value);
            let objectUrl = "";
            if (requestUrl) {
                try {
                    const response = await fetch(requestUrl, {
                        credentials: "same-origin",
                        cache: "no-store",
                        headers: { "Cache-Control": "no-cache" }
                    });
                    if (!response.ok) throw new Error("Voice audio returned HTTP " + response.status);
                    objectUrl = URL.createObjectURL(await response.blob());
                    const played = await new Promise((resolve, reject) => {
                        const audio = new Audio();
                        audio.preload = "auto";
                        audio.src = objectUrl;
                        this._currentVoiceAudio = audio;
                        const timeout = window.setTimeout(() => resolve(false), 90000);
                        const finish = (result, error) => {
                            clearTimeout(timeout);
                            if (error) reject(error);
                            else resolve(result);
                        };
                        audio.addEventListener("ended", () => finish(true), { once: true });
                        audio.addEventListener("pause", () => {
                            if (!audio.ended) finish(false);
                        }, { once: true });
                        audio.addEventListener("error", () => finish(false, new Error("Voice audio could not be decoded.")), { once: true });
                        audio.play().catch(error => finish(false, error));
                    });
                    if (played) return true;
                } catch (error) {
                    console.info("[VoiceAlert] Audio playback failed; trying browser speech:", error);
                }
            }
            if (objectUrl) URL.revokeObjectURL(objectUrl);
            const spoken = await this._speakText(fallbackText);
            const status = dropdown.querySelector(".notification-status");
            if (status && spoken) status.hidden = true;
            if (!spoken && status) {
                status.textContent = "Voice playback is unavailable in this browser. Use a browser with speech synthesis enabled.";
                status.hidden = false;
            }
            return spoken;
        },

        _speakText(value) {
            const text = String(value || "").trim();
            if (!text || !window.speechSynthesis || !window.SpeechSynthesisUtterance) {
                return Promise.resolve(false);
            }
            return new Promise(resolve => {
                try {
                    const utterance = new SpeechSynthesisUtterance(text);
                    const synth = window.speechSynthesis;
                    let finished = false;
                    const timeout = window.setTimeout(() => finish(false), 45000);
                    const finish = result => {
                        if (finished) return;
                        finished = true;
                        window.clearTimeout(timeout);
                        resolve(result);
                    };
                    utterance.onend = () => finish(true);
                    utterance.onerror = () => finish(false);
                    this._currentSpeech = utterance;
                    synth.speak(utterance);
                } catch (_) {
                    resolve(false);
                }
            });
        },

        scheduleRefresh() {
            clearTimeout(this.refreshTimer);
            this.refreshTimer = window.setTimeout(() => this.fetchNotifications(), 100);
        },

        init() {
            if (this.initialized) return;
            this.initialized = true;
            try { localStorage.removeItem(`qad-voice-enabled:${this.userId}`); } catch (_) {}
            // The feed may refresh in the background, but it never opens the
            // drawer or restores an old open state from browser storage.
            dropdown.hidden = true;
            dropdown.setAttribute("aria-hidden", "true");
            btn.setAttribute("aria-expanded", "false");
            this.fetchNotifications();
            this.connectWebSocket();
            const unlockOnInteraction = () => this.unlockVoicePlayback();
            document.addEventListener("pointerdown", unlockOnInteraction, { capture: true });
            document.addEventListener("keydown", unlockOnInteraction, { capture: true });
            window.addEventListener("online", () => {
                this.scheduleRefresh();
            });
            window.setInterval(() => {
                if (!document.hidden) this.fetchNotifications();
            }, 30000);
            btn.addEventListener("click", () => this.fetchNotifications());
            dropdown.addEventListener("click", event => {
                const voiceBtn = event.target.closest("[data-notification-voice]");
                if (voiceBtn) {
                    event.stopPropagation();
                    const src = voiceBtn.dataset.notificationVoice;
                    const text = voiceBtn.dataset.notificationVoiceText || "";
                    if (!src && !text) return;
                    const icon = voiceBtn.querySelector("i");
                    if (icon) icon.className = "fa-solid fa-spinner fa-spin";
                    const resetIcon = () => {
                        if (icon) icon.className = "fa-solid fa-volume-high";
                    };

                    const playback = this._playVoice(src, text);
                    if (this._currentVoiceAudio) {
                        this._currentVoiceAudio.addEventListener("ended", resetIcon, { once: true });
                        this._currentVoiceAudio.addEventListener("pause", resetIcon, { once: true });
                    }
                    playback.finally(resetIcon);
                    return;
                }
                const button = event.target.closest("[data-notification-ack]");
                if (button) {
                    event.preventDefault();
                    event.stopPropagation();
                    this.ack(button.dataset.notificationAck, button, button.dataset.notificationOpen || "");
                }
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
