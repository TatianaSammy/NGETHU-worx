(() => {
    const tabList = document.getElementById("accountDashboardTabs");
    const content = document.getElementById("accounts-dashboard-content");
    if (!tabList || !content) return;

    const paneFor = (target) => {
        if (!target) return null;
        return content.querySelector(target);
    };

    const executeFragmentScripts = (pane, scripts) => {
        for (const oldScript of scripts) {
            const script = document.createElement("script");
            for (const attribute of oldScript.attributes) {
                script.setAttribute(attribute.name, attribute.value);
            }
            script.textContent = oldScript.textContent;
            pane.appendChild(script);
        }
    };

    const loadPane = async (pane, url = pane.dataset.fragmentUrl, force = false) => {
        if (!pane || !url || (pane.dataset.loaded === "true" && !force)) return;
        if (pane.dataset.loading === "true") return;

        pane.dataset.loading = "true";
        pane.setAttribute("aria-busy", "true");
        pane.innerHTML = '<div class="d-flex align-items-center gap-3 py-5 text-muted" role="status"><span class="spinner-border spinner-border-sm" aria-hidden="true"></span><span>Loading section…</span></div>';

        try {
            const response = await fetch(url, {
                credentials: "same-origin",
                headers: { "X-Requested-With": "XMLHttpRequest" },
                cache: "no-store",
            });
            if (!response.ok) throw new Error(`Request failed (${response.status})`);

            const markup = await response.text();
            const parsed = new DOMParser().parseFromString(markup, "text/html");
            const scripts = Array.from(parsed.body.querySelectorAll("script"));
            scripts.forEach((script) => script.remove());
            pane.replaceChildren(...Array.from(parsed.body.childNodes));
            executeFragmentScripts(pane, scripts);
            pane.dataset.loaded = "true";
        } catch (error) {
            console.error("Accounts dashboard section failed to load", error);
            pane.innerHTML = '<div class="alert alert-danger d-flex justify-content-between align-items-center gap-3" role="alert"><span>This section could not be loaded.</span><button type="button" class="btn btn-sm btn-outline-danger" data-dashboard-retry>Try again</button></div>';
            pane.dataset.loaded = "false";
        } finally {
            pane.dataset.loading = "false";
            pane.removeAttribute("aria-busy");
        }
    };

    document.addEventListener("shown.bs.tab", (event) => {
        const target = event.target.getAttribute("data-bs-target");
        const pane = paneFor(target);
        if (pane?.hasAttribute("data-dashboard-fragment")) loadPane(pane);
    });

    content.addEventListener("click", (event) => {
        const retry = event.target.closest("[data-dashboard-retry]");
        if (retry) {
            const pane = retry.closest("[data-dashboard-fragment]");
            if (pane) loadPane(pane, pane.dataset.fragmentUrl, true);
            return;
        }

        const pageLink = event.target.closest("[data-dashboard-page]");
        if (pageLink) {
            event.preventDefault();
            const pane = pageLink.closest("[data-dashboard-fragment]");
            if (pane) loadPane(pane, pageLink.href, true);
        }
    });

    content.addEventListener("submit", (event) => {
        const form = event.target.closest("form[data-dashboard-form]");
        if (!form) return;
        event.preventDefault();
        const pane = form.closest("[data-dashboard-fragment]");
        if (!pane) return;

        const url = new URL(form.action || window.location.href, window.location.href);
        const formData = new FormData(form);
        url.search = new URLSearchParams(formData).toString();
        loadPane(pane, url.toString(), true);
    });

    const initialTarget = window.location.hash;
    if (initialTarget) {
        const link = Array.from(tabList.querySelectorAll('[data-bs-toggle="pill"]'))
            .find((item) => item.getAttribute("data-bs-target") === initialTarget);
        if (link && window.bootstrap?.Tab) window.bootstrap.Tab.getOrCreateInstance(link).show();
    }
})();
