"use strict";

class HeaderController {

    constructor() {

        /* =========================================================
           ELEMENT CACHE (SAFE LAZY RESOLUTION)
        ========================================================= */
        this.topbar = document.querySelector(".topbar");

        this.themeToggle = null;
        this.notificationBtn = null;
        this.notificationDropdown = null;
        this.notificationBadge = null;

        this.userMenuBtn = null;
        this.userDropdown = null;

        /* =========================================================
           STATE
        ========================================================= */
        this.activeDropdown = null;
        this.outsideHandlerActive = false;
        /* Use the shared palette registry; standalone behavior remains a safe fallback. */
        this.themes = window.ThemeEngine && typeof window.ThemeEngine.list === "function"
            ? window.ThemeEngine.list()
            : ["auto", "day", "dawn", "ocean", "emerald", "autumn", "winter", "dusk", "night", "aurora", "emerald-dark", "high-contrast"];
        this.currentThemeIndex = 0;

        /* =========================================================
           BINDINGS
        ========================================================= */
        this.boundOutsideClick = this.handleOutsideClick.bind(this);
        this.boundKeyHandler = this.handleKeydown.bind(this);
        this.boundScrollHandler = this.handleScroll.bind(this);
        this.boundStorageHandler = this.handleStorage.bind(this);

        this.init();
    }

    /* =========================================================
       INIT (DEFER SAFE)
    ========================================================= */
    init() {

        this.cacheElements();          // IMPORTANT FIX
        this.initializeTheme();
        this.bindThemeToggle();
        this.bindDropdowns();
        this.bindGlobalEvents();
        this.bindScrollState();
        this.initializeTooltips();
        this.initializeAccessibility();
    }

    /* =========================================================
       CACHE ELEMENTS (FIX: prevents null binding issues)
    ========================================================= */
    cacheElements() {

        this.themeToggle = document.getElementById("themeToggle");

        this.notificationBtn = document.getElementById("notificationBtn");
        this.notificationDropdown = document.getElementById("notificationDropdown");
        this.notificationBadge = document.getElementById("notificationBadge");

        this.userMenuBtn = document.getElementById("userMenuBtn");
        this.userDropdown = document.getElementById("userDropdown");
    }

    /* =========================================================
       ACCESSIBILITY
    ========================================================= */
    initializeAccessibility() {
        if (this.userMenuBtn)
            this.userMenuBtn.setAttribute("aria-expanded", "false");

        if (this.notificationBtn)
            this.notificationBtn.setAttribute("aria-expanded", "false");
    }

    /* =========================================================
       DROPDOWN CORE ENGINE (FIXED)
    ========================================================= */
    bindDropdowns() {

        this.bindUserMenuDialog();
        this.bindDropdown(this.notificationBtn, this.notificationDropdown);
    }

    bindUserMenuDialog() {
        if (!this.userMenuBtn || !this.userDropdown || typeof this.userDropdown.showModal !== "function") return;

        this.userDropdown.addEventListener("close", () => {
            this.userMenuBtn.setAttribute("aria-expanded", "false");
        });
        this.userDropdown.addEventListener("click", (event) => {
            if (event.target === this.userDropdown || event.target.closest("[data-close-user-menu]")) {
                this.closeUserMenu();
            }
        });
        this.userMenuBtn.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            if (this.userDropdown.open) {
                this.closeUserMenu();
            } else {
                this.openUserMenu();
            }
        });
    }

    openUserMenu() {
        if (!this.userDropdown || this.userDropdown.open) return;
        this.closeAllDropdowns();
        this.userDropdown.showModal();
        this.userMenuBtn.setAttribute("aria-expanded", "true");
        const firstAction = this.userDropdown.querySelector("[data-close-user-menu], [role='menuitem']");
        if (firstAction) firstAction.focus({ preventScroll: true });
    }

    closeUserMenu(restoreFocus = true) {
        if (this.userDropdown && this.userDropdown.open) this.userDropdown.close();
        if (this.userMenuBtn) {
            this.userMenuBtn.setAttribute("aria-expanded", "false");
            if (restoreFocus) this.userMenuBtn.focus({ preventScroll: true });
        }
    }

    bindDropdown(button, dropdown) {

        if (!button || !dropdown) return;

        // enforce class consistency
        dropdown.classList.add("dropdown-menu");
        dropdown.classList.remove("show");
        dropdown.hidden = true;
        dropdown.setAttribute("aria-hidden", "true");
        button.setAttribute("aria-expanded", "false");

        button.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();

            this.toggleDropdown(button, dropdown);
        });
    }

    toggleDropdown(button, dropdown) {

        const isOpen = dropdown.classList.contains("show") && !dropdown.hidden;

        this.closeAllDropdowns();

        if (isOpen) return;

        dropdown.classList.add("show");
        dropdown.hidden = false;
        dropdown.setAttribute("aria-hidden", "false");
        button.setAttribute("aria-expanded", "true");

        this.activeDropdown = dropdown;

        if (!this.outsideHandlerActive) {
            document.addEventListener("click", this.boundOutsideClick);
            this.outsideHandlerActive = true;
        }
    }

    closeAllDropdowns() {

        this.closeUserMenu(false);

        document.querySelectorAll(".dropdown-menu.show")
            .forEach(el => el.classList.remove("show"));
        if (this.notificationDropdown) {
            this.notificationDropdown.hidden = true;
            this.notificationDropdown.setAttribute("aria-hidden", "true");
        }

        document.querySelectorAll("[aria-expanded='true']")
            .forEach(btn => btn.setAttribute("aria-expanded", "false"));

        this.activeDropdown = null;

        if (this.outsideHandlerActive) {
            document.removeEventListener("click", this.boundOutsideClick);
            this.outsideHandlerActive = false;
        }
    }

    handleOutsideClick(e) {

        if (!this.activeDropdown) return;

        const clickedInside = this.activeDropdown.contains(e.target);

        const clickedButton =
            (this.userMenuBtn && this.userMenuBtn.contains(e.target)) ||
            (this.notificationBtn && this.notificationBtn.contains(e.target));

        if (clickedInside || clickedButton) return;

        this.closeAllDropdowns();
    }

    /* =========================================================
       NOTIFICATIONS
    ========================================================= */
    updateNotificationBadge(count = 0) {

        if (!this.notificationBadge) return;

        if (count <= 0) {
            this.notificationBadge.style.display = "none";
            return;
        }

        this.notificationBadge.style.display = "grid";
        this.notificationBadge.textContent = count > 99 ? "99+" : String(count);
    }

    /* =========================================================
       KEYBOARD SUPPORT
    ========================================================= */
    handleKeydown(e) {

        if (e.key === "Escape") {
            this.closeAllDropdowns();
        }

        if (e.key.toLowerCase() === "t") {
            this.nextTheme();
        }
    }

    /* =========================================================
       SCROLL EFFECT
    ========================================================= */
    bindScrollState() {

        window.addEventListener("scroll", this.boundScrollHandler, { passive: true });

        this.handleScroll();
    }

    handleScroll() {

        if (!this.topbar) return;

        this.topbar.classList.toggle("scrolled", window.scrollY > 10);
    }

    /* =========================================================
       THEME ENGINE (SAFE + FIXED)
    ========================================================= */
    initializeTheme() {
        /* Defer to the shared ThemeEngine so every app uses the same palette. */
        if (window.ThemeEngine && typeof window.ThemeEngine.get === "function") {
            const current = window.ThemeEngine.get();
            const mode = typeof window.ThemeEngine.getMode === "function" ? window.ThemeEngine.getMode() : "manual";
            this.currentThemeIndex = Math.max(0, this.themes.indexOf(mode === "auto" ? "auto" : current));
            this.updateToggleIcon(current, mode);
            return;
        }
    }

    bindThemeToggle() {
        /* ThemeEngine owns the click handler; header only syncs icon + flash */
        if (!this.themeToggle) return;
        window.addEventListener("themeChanged", (e) => {
            const theme = (e.detail && e.detail.theme) || "day";
            const mode = (e.detail && e.detail.mode) || "manual";
            this.currentThemeIndex = Math.max(0, this.themes.indexOf(mode === "auto" ? "auto" : theme));
            this.updateToggleIcon(theme, mode);
        });
    }

    nextTheme(x = 0.5, y = 0.5) {
        if (window.ThemeEngine && typeof window.ThemeEngine.cycle === "function") {
            const flash = this.createFlash();
            this.triggerPortalEffect(flash, x, y);
            window.ThemeEngine.cycle();
            return;
        }
    }

    applyTheme(theme, animate = true, x = 0.5, y = 0.5) {
        if (window.ThemeEngine && typeof window.ThemeEngine.set === "function") {
            if (animate) {
                const flash = this.createFlash();
                this.triggerPortalEffect(flash, x, y);
            }
            window.ThemeEngine.set(theme);
            return;
        }
    }

    /* =========================================================
       PORTAL EFFECT
    ========================================================= */
    createFlash() {

        let flash = document.querySelector(".theme-flash");

        if (!flash) {
            flash = document.createElement("div");
            flash.className = "theme-flash";
            document.body.appendChild(flash);
        }

        return flash;
    }

    triggerPortalEffect(flash, x, y) {

        flash.style.setProperty("--x", `${x * 100}%`);
        flash.style.setProperty("--y", `${y * 100}%`);

        flash.classList.add("active");

        setTimeout(() => {
            flash.classList.remove("active");
        }, 450);
    }

    /* =========================================================
       ICON UPDATE
    ========================================================= */
    updateToggleIcon(theme, mode = "manual") {

        if (!this.themeToggle) return;

        const icon = this.themeToggle.querySelector("i");

        if (!icon) return;

        const ICON_MAP = {
            "auto": "fas fa-satellite-dish",
            "day": "fas fa-sun",
            "daylight": "fas fa-sun",
            "dawn": "fas fa-sun",
            "light": "fas fa-sun",
            "default": "fas fa-sun",
            "ocean": "fas fa-water",
            "emerald": "fas fa-leaf",
            "autumn": "fas fa-leaf",
            "winter": "fas fa-snowflake",
            "emerald-dark": "fas fa-leaf",
            "cyber": "fas fa-terminal",
            "night": "fas fa-terminal",
            "dark": "fas fa-terminal",
            "abyss": "fas fa-circle",
            "celestial": "fas fa-star",
            "aurora": "fas fa-star",
            "inferno": "fas fa-moon",
            "gold": "fas fa-moon",
            "dusk": "fas fa-cloud-moon",
            "oled": "fas fa-circle",
            "amoled": "fas fa-circle",
            "high-contrast": "fas fa-shield-halved",
            "solar": "fas fa-shield-halved"
        };

        icon.className = mode === "auto" ? ICON_MAP.auto : (ICON_MAP[theme] || "fas fa-sun");
    }

    /* =========================================================
       GLOBAL EVENTS
    ========================================================= */
    bindGlobalEvents() {

        document.addEventListener("keydown", this.boundKeyHandler);
        window.addEventListener("storage", this.boundStorageHandler);
        // BFCache can restore a page with a menu visibly open. Keep drawers
        // transient across page navigation and PWA resume.
        window.addEventListener("pageshow", () => this.closeAllDropdowns());
        window.addEventListener("popstate", () => this.closeAllDropdowns());
        document.addEventListener("htmx:beforeRequest", () => this.closeAllDropdowns());

        document.addEventListener("mousemove", (e) => {
            document.documentElement.style.setProperty("--x", `${e.clientX}px`);
            document.documentElement.style.setProperty("--y", `${e.clientY}px`);
        });
    }

    handleStorage(event) {

        if (!window.ThemeEngine || typeof window.ThemeEngine.get !== "function") return;
        const theme = window.ThemeEngine.get();
        const mode = typeof window.ThemeEngine.getMode === "function" ? window.ThemeEngine.getMode() : "manual";
        this.currentThemeIndex = Math.max(0, this.themes.indexOf(mode === "auto" ? "auto" : theme));
        this.updateToggleIcon(theme, mode);
    }

    /* =========================================================
       TOOLTIP CLEANUP
    ========================================================= */
    initializeTooltips() {

        document.querySelectorAll("[title]").forEach(el => {

            el.dataset.tooltip = el.getAttribute("title");
            el.removeAttribute("title");
        });
    }
}

/* =========================================================
   BOOTSTRAP
========================================================= */
document.addEventListener("DOMContentLoaded", () => {
    window.headerController = new HeaderController();
});
