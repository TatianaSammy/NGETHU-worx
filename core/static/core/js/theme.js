/**
 * NCWSCwaterLAB - Advanced Futuristic 24/7 Theme Engine & Telemetry Console.
 * Engineered for mission-critical water utility command centers, outdoor field operations,
 * and multi-shift 24/7 telemetry monitoring.
 *
 * Provides extreme performance, zero rendering lag, and ultra-high contrast across
 * all lighting conditions: Direct Sun / Inclement Weather / Midnight Control Room.
 */
(function () {
    'use strict';

    var THEMES = [
        {
            name: 'day',
            badge: 'DAYLIGHT',
            label: 'Daylight Enterprise',
            desc: 'Pristine daylight palette with high-contrast slate typography',
            icon: 'fas fa-sun',
            color: '#0284c7',
            border: '#93c5fd',
            scheme: 'light',
            category: 'standard'
        },
        {
            name: 'cyber',
            badge: 'CYBER',
            label: 'Cyber Command (Midnight)',
            desc: 'Futuristic obsidian canvas, luminous cyan telemetry & sharp contrast',
            icon: 'fas fa-terminal',
            color: '#00e5ff',
            border: '#00e5ff',
            scheme: 'dark',
            category: 'futuristic'
        },
        {
            name: 'solar',
            badge: 'SOLAR',
            label: 'Solar Glare (Field High Contrast)',
            desc: 'Blinding outdoor sunlight mode with maximal contrast (12:1+ ratio)',
            icon: 'fas fa-shield-halved',
            color: '#005fcc',
            border: '#000000',
            scheme: 'light',
            category: 'contrast'
        },
        {
            name: 'abyss',
            badge: 'ABYSS',
            label: 'Deep Abyss (OLED Pitch Black)',
            desc: 'Zero-power OLED true black with crisp luminescent readouts',
            icon: 'fas fa-circle',
            color: '#38bdf8',
            border: '#1e293b',
            scheme: 'dark',
            category: 'dark'
        },
        {
            name: 'aurora',
            badge: 'AURORA',
            label: 'Quantum Aurora (Sci-Fi Glass)',
            desc: 'Cosmic deep space indigo, violet & cyan aurora glowing HUD',
            icon: 'fas fa-star',
            color: '#a855f7',
            border: '#c084fc',
            scheme: 'dark',
            category: 'futuristic'
        },
        {
            name: 'ocean',
            badge: 'OCEAN',
            label: 'Summer Ocean',
            desc: 'Clear daylight with azure water treatment accents',
            icon: 'fas fa-water',
            color: '#0284c7',
            border: '#7dd3fc',
            scheme: 'light',
            category: 'standard'
        },
        {
            name: 'emerald',
            badge: 'SPRING',
            label: 'Spring Emerald',
            desc: 'Crisp botanical light theme with vibrant biological green tones',
            icon: 'fas fa-leaf',
            color: '#16a34a',
            border: '#86efac',
            scheme: 'light',
            category: 'standard'
        },
        {
            name: 'emerald-dark',
            badge: 'EMERALD',
            label: 'Emerald Biosphere',
            desc: 'Tactical eco night mode with high-contrast biological green markers',
            icon: 'fas fa-seedling',
            color: '#10b981',
            border: '#34d399',
            scheme: 'dark',
            category: 'dark'
        },
        {
            name: 'dawn',
            badge: 'DAWN',
            label: 'Dawn Golden Sunrise',
            desc: 'Warm golden amber morning sunlight palette',
            icon: 'fas fa-cloud-sun',
            color: '#f59e0b',
            border: '#fde68a',
            scheme: 'light',
            category: 'standard'
        },
        {
            name: 'dusk',
            badge: 'DUSK',
            label: 'Dusk Handover',
            desc: 'Warm amber shift-transition palette for twilight & evening handovers',
            icon: 'fas fa-cloud-moon',
            color: '#f59e0b',
            border: '#fbbf24',
            scheme: 'dark',
            category: 'shift'
        },
        {
            name: 'autumn',
            badge: 'AUTUMN',
            label: 'Autumn Earth',
            desc: 'Warm terracotta and earth palette for calm operational work',
            icon: 'fas fa-feather',
            color: '#c2410c',
            border: '#fdba74',
            scheme: 'light',
            category: 'standard'
        },
        {
            name: 'winter',
            badge: 'WINTER',
            label: 'Winter Ice',
            desc: 'Cool crystalline daylight with crisp slate contrast',
            icon: 'fas fa-snowflake',
            color: '#3b82f6',
            border: '#bfdbfe',
            scheme: 'light',
            category: 'standard'
        },
        {
            name: 'night',
            badge: 'NIGHT',
            label: 'Midnight Observatory',
            desc: 'Deep navy night shift observatory theme for low fatigue',
            icon: 'fas fa-moon',
            color: '#60a5fa',
            border: '#1e3a8a',
            scheme: 'dark',
            category: 'dark'
        }
    ];

    var AUTO = {
        name: 'auto',
        badge: '24/7 AUTO',
        label: '24/7 Shift Telemetry Sync',
        desc: 'Intelligent real-time sync with Day, Evening & Midnight duty shifts',
        icon: 'fas fa-satellite-dish',
        color: '#10b981',
        scheme: 'auto'
    };

    var THEME_NAMES = THEMES.map(function (t) { return t.name; });
    var CYCLE_NAMES = THEME_NAMES.concat(['auto']);
    var THEME_BY_NAME = THEMES.reduce(function (map, theme) {
        map[theme.name] = theme;
        return map;
    }, {});

    // Comprehensive Aliases for complete backward compatibility across all apps
    var ALIASES = {
        'default': 'day', 'standard': 'day', 'enterprise': 'day', 'light': 'day', 'daylight': 'day',
        'night': 'cyber', 'dark': 'cyber', 'cyber-command': 'cyber', 'command': 'cyber',
        'high-contrast': 'solar', 'contrast': 'solar', 'sunlight': 'solar',
        'oled': 'abyss', 'amoled': 'abyss', 'black': 'abyss', 'pitch': 'abyss',
        'celestial': 'aurora', 'scifi': 'aurora', 'quantum': 'aurora',
        'summer': 'ocean', 'spring': 'day', 'gold': 'dusk', 'inferno': 'dusk',
        'winter': 'day'
    };

    var root = document.documentElement;
    var STORAGE_KEY = 'qad_theme';
    var LEGACY_STORAGE_KEY = 'theme';
    var MANUAL_KEY = 'qad_theme_manual';
    var mode = 'auto';

    var darkThemes = ['cyber', 'abyss', 'aurora', 'emerald-dark', 'dusk', 'night', 'dark', 'oled'];

    function normalize(name) {
        name = String(name || '').trim().toLowerCase();
        if (name === 'auto') return 'auto';
        if (ALIASES[name]) return ALIASES[name];
        return THEME_BY_NAME[name] ? name : 'day';
    }

    function mediaMatches(query) {
        try { return Boolean(window.matchMedia && window.matchMedia(query).matches); }
        catch (e) { return false; }
    }

    function detectActiveShift() {
        // Read active shift name from header shift telemetry if present
        var shiftEl = document.getElementById('headerShiftName');
        var shiftText = shiftEl ? shiftEl.textContent.trim().toLowerCase() : '';
        var hour = new Date().getHours();

        if (shiftText.indexOf('midnight') !== -1 || shiftText.indexOf('night') !== -1) {
            return { name: 'Midnight Shift', type: 'midnight', defaultTheme: 'cyber' };
        }
        if (shiftText.indexOf('evening') !== -1 || shiftText.indexOf('dusk') !== -1) {
            return { name: 'Evening Shift', type: 'evening', defaultTheme: 'dusk' };
        }
        if (shiftText.indexOf('day') !== -1 || shiftText.indexOf('morning') !== -1) {
            return { name: 'Day Shift', type: 'day', defaultTheme: 'day' };
        }

        // Time-based fallback for 24/7 duty roster shifts
        // Day Shift: 08:00 - 16:00 | Evening Shift: 16:00 - 00:00 | Midnight Shift: 00:00 - 08:00
        if (hour >= 8 && hour < 16) {
            return { name: 'Day Shift (08:00–16:00)', type: 'day', defaultTheme: 'day' };
        } else if (hour >= 16 && hour < 24) {
            return { name: 'Evening Shift (16:00–00:00)', type: 'evening', defaultTheme: 'dusk' };
        } else {
            return { name: 'Midnight Shift (00:00–08:00)', type: 'midnight', defaultTheme: 'cyber' };
        }
    }

    function automaticTheme() {
        if (mediaMatches('(prefers-contrast: more)')) return 'solar';

        var shift = detectActiveShift();
        var darkPreference = mediaMatches('(prefers-color-scheme: dark)');

        if (shift.type === 'midnight') return 'cyber';
        if (shift.type === 'evening') return darkPreference ? 'cyber' : 'dusk';
        return darkPreference ? 'cyber' : 'day';
    }

    function updateMetaThemeColor(name) {
        var meta = document.querySelector('meta[name="theme-color"]');
        var theme = THEME_BY_NAME[name];
        if (meta && theme) meta.setAttribute('content', theme.color);
    }

    function updateToggleButtons(name) {
        var currentTheme = THEME_BY_NAME[name] || THEMES[0];
        var isAuto = (mode === 'auto');
        var badgeLabel = isAuto ? '24/7 AUTO' : currentTheme.badge;
        var displayTitle = isAuto
            ? '24/7 Shift Telemetry Sync · Active: ' + currentTheme.label
            : 'Operational Theme: ' + currentTheme.label;

        // 1. Update Futuristic Theme HUD Pill in Header
        var badgeEl = document.getElementById('themeHudBadge');
        var orbEl = document.getElementById('themeHudOrb');
        var pulseEl = document.getElementById('themeHudPulse');
        var shiftEl = document.getElementById('hudTelemetryShift');

        if (badgeEl) badgeEl.textContent = badgeLabel;
        if (orbEl) {
            orbEl.innerHTML = '<i class="' + (isAuto ? AUTO.icon : currentTheme.icon) + '" aria-hidden="true"></i>';
            orbEl.style.color = currentTheme.color;
        }
        if (pulseEl) {
            pulseEl.className = 'theme-hud-pulse ' + (isAuto ? 'is-auto' : 'is-manual');
            pulseEl.title = isAuto ? '24/7 Telemetry Shift Sensor Active' : 'Manual Override Active';
        }
        if (shiftEl) {
            var shiftInfo = detectActiveShift();
            shiftEl.innerHTML = '<i class="fas fa-satellite-dish me-1"></i>' + (isAuto ? shiftInfo.name : 'Manual: ' + currentTheme.badge);
        }

        // 2. Update all buttons with theme toggle attributes
        document.querySelectorAll('#themeToggle, .theme-toggle, [data-theme-toggle]').forEach(function (button) {
            button.setAttribute('title', displayTitle + ' · Click to toggle or open HUD (Alt+T)');
            button.setAttribute('aria-label', displayTitle);
            button.setAttribute('data-current-theme', name);
            button.setAttribute('data-theme-mode', mode);
            var icon = button.querySelector('i');
            if (icon && !button.classList.contains('theme-hud-btn')) {
                icon.className = isAuto ? AUTO.icon : currentTheme.icon;
            }
        });

        // 3. Update active card in popover menu
        document.querySelectorAll('.hud-preset-card').forEach(function (card) {
            var cardTheme = card.getAttribute('data-theme-choice');
            var isActive = isAuto ? (cardTheme === 'auto') : (cardTheme === name);
            card.classList.toggle('is-active', isActive);
            var radio = card.querySelector('.hud-preset-radio');
            if (radio) radio.innerHTML = isActive ? '<i class="fas fa-check-circle"></i>' : '<i class="far fa-circle"></i>';
        });
    }

    function applyTheme(name, options) {
        options = options || {};
        name = normalize(name);
        if (name === 'auto') name = automaticTheme();
        var isDark = darkThemes.indexOf(name) !== -1;

        root.setAttribute('data-theme', name);
        root.setAttribute('data-theme-mode', mode);
        root.classList.toggle('theme-day', name === 'day');
        root.classList.toggle('theme-cyber', name === 'cyber');
        root.classList.toggle('theme-solar', name === 'solar');
        root.classList.toggle('theme-night', isDark);
        root.classList.toggle('theme-light', !isDark);
        root.classList.toggle('theme-dark', isDark);
        root.classList.toggle('theme-auto', mode === 'auto');

        if (options.persist !== false) {
            try {
                var storedName = mode === 'auto' ? 'auto' : name;
                localStorage.setItem(STORAGE_KEY, storedName);
                localStorage.setItem(LEGACY_STORAGE_KEY, storedName);
                if (mode === 'auto') localStorage.removeItem(MANUAL_KEY);
                else localStorage.setItem(MANUAL_KEY, 'true');
            } catch (e) { /* Storage might be disabled */ }
        }

        updateToggleButtons(name);
        updateMetaThemeColor(name);

        if (options.dispatch !== false) {
            window.dispatchEvent(new CustomEvent('themeChanged', {
                detail: { theme: name, mode: mode, scheme: isDark ? 'dark' : 'light' }
            }));
        }
        return name;
    }

    function setAutomatic(options) {
        mode = 'auto';
        return applyTheme(automaticTheme(), options);
    }

    function setTheme(name) {
        name = normalize(name);
        if (name === 'auto') return setAutomatic();
        mode = 'manual';
        return applyTheme(name);
    }

    function cycleTheme() {
        var current = mode === 'auto' ? 'auto' : normalize(root.getAttribute('data-theme'));
        var currentIndex = CYCLE_NAMES.indexOf(current);
        var nextName = CYCLE_NAMES[(currentIndex + 1) % CYCLE_NAMES.length];
        var result;
        if (nextName === 'auto') {
            result = setAutomatic();
            showQuickNotification('Theme: 24/7 Shift Telemetry Sync [Auto]');
        } else {
            result = setTheme(nextName);
            var tObj = THEME_BY_NAME[nextName];
            showQuickNotification('Theme: ' + (tObj ? tObj.label : nextName));
        }
        return result;
    }

    function quickToggle() {
        return cycleTheme();
    }

    function showQuickNotification(msg) {
        var toast = document.getElementById('themeToastNotification');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'themeToastNotification';
            toast.className = 'theme-toast-notification';
            document.body.appendChild(toast);
        }
        toast.textContent = msg;
        toast.classList.add('is-visible');
        clearTimeout(toast._timer);
        toast._timer = setTimeout(function () {
            toast.classList.remove('is-visible');
        }, 1800);
    }

    function readSavedTheme() {
        try {
            return localStorage.getItem(STORAGE_KEY) ||
                localStorage.getItem(LEGACY_STORAGE_KEY) ||
                localStorage.getItem('qca_theme');
        } catch (e) {
            return null;
        }
    }

    function buildPopoverGrid() {
        var grid = document.getElementById('hudPresetsGrid');
        if (!grid || grid.dataset.built === 'true') return;
        grid.dataset.built = 'true';

        var html = '';

        // Auto card first
        html += '<button type="button" class="hud-preset-card hud-preset-auto" data-theme-choice="auto">';
        html += '  <div class="hud-preset-swatch" style="background: linear-gradient(135deg, #0284c7 0%, #00e5ff 50%, #10b981 100%);"></div>';
        html += '  <div class="hud-preset-info">';
        html += '    <div class="hud-preset-name">' + AUTO.label + '</div>';
        html += '    <div class="hud-preset-desc">' + AUTO.desc + '</div>';
        html += '  </div>';
        html += '  <div class="hud-preset-radio"><i class="far fa-circle"></i></div>';
        html += '</button>';

        // Grid of manual operational presets
        THEMES.forEach(function (t) {
            html += '<button type="button" class="hud-preset-card" data-theme-choice="' + t.name + '">';
            html += '  <div class="hud-preset-swatch" style="background-color: ' + t.color + '; border-color: ' + t.border + ';">';
            html += '    <i class="' + t.icon + '"></i>';
            html += '  </div>';
            html += '  <div class="hud-preset-info">';
            html += '    <div class="hud-preset-name"><span class="hud-mini-badge">' + t.badge + '</span> ' + t.label + '</div>';
            html += '    <div class="hud-preset-desc">' + t.desc + '</div>';
            html += '  </div>';
            html += '  <div class="hud-preset-radio"><i class="far fa-circle"></i></div>';
            html += '</button>';
        });

        grid.innerHTML = html;

        // Wire click handlers for preset cards
        grid.querySelectorAll('.hud-preset-card').forEach(function (card) {
            card.addEventListener('click', function (e) {
                e.preventDefault();
                var chosen = card.getAttribute('data-theme-choice');
                if (chosen === 'auto') {
                    setAutomatic();
                    showQuickNotification('24/7 Shift Telemetry Sync Activated');
                } else {
                    setTheme(chosen);
                    var tObj = THEME_BY_NAME[chosen];
                    showQuickNotification('Theme: ' + (tObj ? tObj.label : chosen));
                }
                closePopover();
            });
        });
    }

    function openPopover() {
        var popover = document.getElementById('themeHudPopover');
        var btn = document.getElementById('themeToggle');
        if (!popover) return;
        buildPopoverGrid();
        updateToggleButtons(normalize(root.getAttribute('data-theme')));
        popover.classList.add('is-open');
        popover.setAttribute('aria-hidden', 'false');
        if (btn) btn.setAttribute('aria-expanded', 'true');
    }

    function closePopover() {
        var popover = document.getElementById('themeHudPopover');
        var btn = document.getElementById('themeToggle');
        if (!popover) return;
        popover.classList.remove('is-open');
        popover.setAttribute('aria-hidden', 'true');
        if (btn) btn.setAttribute('aria-expanded', 'false');
    }

    function togglePopover() {
        var popover = document.getElementById('themeHudPopover');
        if (!popover) return;
        if (popover.classList.contains('is-open')) closePopover();
        else openPopover();
    }

    function wireButtons() {
        var mainToggleBtn = document.getElementById('themeToggle');

        if (mainToggleBtn && mainToggleBtn.dataset.themeEngineBound !== 'true') {
            mainToggleBtn.dataset.themeEngineBound = 'true';
            mainToggleBtn.addEventListener('click', function (e) {
                e.preventDefault();
                quickToggle();
            });
        }

        // Wire legacy buttons
        document.querySelectorAll('.theme-toggle:not(#themeToggle), [data-theme-toggle]').forEach(function (button) {
            if (button.dataset.themeEngineBound === 'true') return;
            button.dataset.themeEngineBound = 'true';
            button.addEventListener('click', function (e) {
                e.preventDefault();
                quickToggle();
            });
        });

        document.addEventListener('keydown', function (e) {
            // Global Keyboard Shortcut: Alt + T to quick-toggle Day/Cyber
            if (e.altKey && (e.key === 't' || e.key === 'T')) {
                e.preventDefault();
                quickToggle();
            }
        });

        updateToggleButtons(normalize(root.getAttribute('data-theme')));
    }

    function refreshAutomaticTheme() {
        if (mode === 'auto') {
            applyTheme(automaticTheme(), { dispatch: true, persist: false });
        }
    }

    function init() {
        var saved = readSavedTheme();
        mode = saved && normalize(saved) !== 'auto' ? 'manual' : 'auto';
        applyTheme(mode === 'auto' ? automaticTheme() : normalize(saved), {
            dispatch: false,
            persist: true
        });
        root.classList.add('fullscreen-app');
    }

    window.ThemeEngine = {
        set: setTheme,
        cycle: cycleTheme,
        toggle: quickToggle,
        get: function () { return normalize(root.getAttribute('data-theme')); },
        getMode: function () { return mode; },
        list: function () { return CYCLE_NAMES.slice(); },
        normalize: normalize,
        resetAuto: function () { return setAutomatic(); },
        openHUD: openPopover,
        closeHUD: closePopover
    };

    // Initialize immediately to prevent flash of wrong theme
    init();

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', wireButtons, { once: true });
    } else {
        wireButtons();
    }

    // Cross-tab storage synchronization
    window.addEventListener('storage', function (event) {
        if (event.key === STORAGE_KEY || event.key === LEGACY_STORAGE_KEY || event.key === 'qca_theme') {
            var next = event.newValue ? normalize(event.newValue) : 'auto';
            if (next === 'auto') setAutomatic({ dispatch: true, persist: false });
            else {
                mode = 'manual';
                applyTheme(next, { dispatch: true, persist: false });
            }
        }
    });

    // Listen to OS Dark Mode and Contrast changes
    ['(prefers-color-scheme: dark)', '(prefers-contrast: more)'].forEach(function (query) {
        try {
            var media = window.matchMedia && window.matchMedia(query);
            if (media && typeof media.addEventListener === 'function') {
                media.addEventListener('change', refreshAutomaticTheme);
            } else if (media && typeof media.addListener === 'function') {
                media.addListener(refreshAutomaticTheme);
            }
        } catch (e) { }
    });

    // Check shift schedule periodically every 60s
    window.setInterval(refreshAutomaticTheme, 60000);
})();
