(() => {
    const root = document.querySelector("[data-microbial-workbook]");
    if (!root) return;

    const tabs = [...root.querySelectorAll("[data-workbook-tab]")];
    const panels = [...root.querySelectorAll("[data-workbook-panel]")];
    let hasUnsavedInput = false;

    const activate = (reference, updateUrl = true) => {
        const found = tabs.some((tab) => tab.dataset.workbookTab === reference);
        const active = found ? reference : tabs[0]?.dataset.workbookTab;
        if (!active) return;

        tabs.forEach((tab) => {
            const selected = tab.dataset.workbookTab === active;
            tab.setAttribute("aria-selected", String(selected));
            tab.tabIndex = selected ? 0 : -1;
        });
        panels.forEach((panel) => {
            panel.hidden = panel.dataset.workbookPanel !== active;
        });
        const selectedPanel = panels.find((panel) => panel.dataset.workbookPanel === active);
        const analysisInput = root.querySelector('.microbial-workbook__filters input[name="analysis"]');
        if (analysisInput) analysisInput.value = active;

        if (updateUrl) {
            const url = new URL(window.location.href);
            url.searchParams.set("analysis", active);
            window.history.replaceState({}, "", url);
        }

        const returnInput = selectedPanel?.querySelector('input[name="return_to"]');
        if (returnInput) {
            const url = new URL(window.location.href);
            url.searchParams.set("analysis", active);
            returnInput.value = `${url.pathname}${url.search}#${selectedPanel.id}`;
        }
    };

    tabs.forEach((tab, index) => {
        tab.addEventListener("click", () => activate(tab.dataset.workbookTab));
        tab.addEventListener("keydown", (event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const nextIndex = event.key === "Home" ? 0
                : event.key === "End" ? tabs.length - 1
                    : (index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
            tabs[nextIndex]?.focus();
            activate(tabs[nextIndex]?.dataset.workbookTab);
        });
    });

    const requested = new URL(window.location.href).searchParams.get("analysis");
    activate(requested || tabs.find((tab) => tab.getAttribute("aria-selected") === "true")?.dataset.workbookTab, false);

    root.querySelector("[data-workbook-refresh]")?.addEventListener("click", () => {
        window.location.reload();
    });

    root.addEventListener("input", (event) => {
        if (event.target.matches('.microbial-matrix__input input, .microbial-matrix__methods select')) {
            hasUnsavedInput = true;
        }
    });
    root.querySelectorAll("form").forEach((form) => {
        form.addEventListener("reset", () => {
            window.setTimeout(() => { hasUnsavedInput = false; }, 0);
        });
    });
    window.setTimeout(function refreshWhenIdle() {
        if (document.visibilityState === "visible" && !hasUnsavedInput) {
            window.location.reload();
            return;
        }
        window.setTimeout(refreshWhenIdle, 10000);
    }, 60000);
})();
