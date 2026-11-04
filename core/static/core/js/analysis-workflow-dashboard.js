(() => {
    if (window.__analysisWorkflowDashboard) return;
    window.__analysisWorkflowDashboard = true;

    const readJson = (id) => {
        const node = document.getElementById(id);
        if (!node) return [];
        try {
            return JSON.parse(node.textContent || "[]");
        } catch {
            return [];
        }
    };

    const palette = {
        grid: "rgba(118, 143, 161, .2)",
        muted: "#8091a0",
        teal: "#22c8b5",
        blue: "#5b9df4",
        green: "#24b889",
        red: "#e76d7d",
        amber: "#e9a73c",
        violet: "#9c82ef",
    };

    const root = () => document.querySelector("[data-analysis-dashboard]");

    const prepareCanvas = (canvas) => {
        const bounds = canvas.getBoundingClientRect();
        if (!bounds.width || !bounds.height) return null;
        const scale = Math.min(Math.max(window.devicePixelRatio || 1, 1), 2);
        canvas.width = Math.round(bounds.width * scale);
        canvas.height = Math.round(bounds.height * scale);
        const ctx = canvas.getContext("2d");
        if (!ctx) return null;
        ctx.setTransform(scale, 0, 0, scale, 0, 0);
        ctx.clearRect(0, 0, bounds.width, bounds.height);
        return { ctx, width: bounds.width, height: bounds.height };
    };

    const showTooltip = (canvas, event, text) => {
        const tooltip = canvas.parentElement?.querySelector(".analysis-tooltip");
        if (!tooltip) return;
        if (!text) {
            tooltip.hidden = true;
            return;
        }
        tooltip.textContent = text;
        tooltip.hidden = false;
        const bounds = canvas.getBoundingClientRect();
        const x = event.clientX - bounds.left;
        const y = event.clientY - bounds.top;
        tooltip.style.left = `${Math.max(4, Math.min(x + 12, bounds.width - tooltip.offsetWidth - 4))}px`;
        tooltip.style.top = `${Math.max(4, Math.min(y - 14, bounds.height - tooltip.offsetHeight - 4))}px`;
    };

    const attachTooltip = (canvas) => {
        if (canvas.dataset.tooltipReady) return;
        canvas.dataset.tooltipReady = "true";
        canvas.addEventListener("pointermove", (event) => {
            const state = canvas._analysisChartState;
            if (!state?.points?.length) return;
            const bounds = canvas.getBoundingClientRect();
            const x = event.clientX - bounds.left;
            const point = state.points.reduce((nearest, candidate) => (
                Math.abs(candidate.x - x) < Math.abs(nearest.x - x) ? candidate : nearest
            ), state.points[0]);
            showTooltip(canvas, event, point?.tooltip || "");
        });
        canvas.addEventListener("pointerleave", () => showTooltip(canvas, null, ""));
    };

    const finishCanvas = (canvas, layout, state) => {
        canvas._analysisChartState = state;
        attachTooltip(canvas);
        return layout;
    };

    const drawLine = (canvas, data, color, label) => {
        const layout = prepareCanvas(canvas);
        if (!layout) return;
        const { ctx, width, height } = layout;
        const pad = { left: 34, right: 9, top: 12, bottom: 27 };
        const plotWidth = Math.max(20, width - pad.left - pad.right);
        const plotHeight = Math.max(24, height - pad.top - pad.bottom);
        const maxValue = Math.max(1, ...data.map((row) => Number(row.count || 0)));
        ctx.font = "10px system-ui, sans-serif";
        ctx.textBaseline = "middle";
        ctx.strokeStyle = palette.grid;
        ctx.fillStyle = palette.muted;
        for (let index = 0; index <= 4; index += 1) {
            const y = pad.top + (plotHeight * index) / 4;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(width - pad.right, y);
            ctx.stroke();
            ctx.textAlign = "right";
            ctx.fillText(String(Math.round(maxValue * (1 - index / 4))), pad.left - 7, y);
        }
        const points = [];
        const gradient = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
        gradient.addColorStop(0, `${color}32`);
        gradient.addColorStop(1, `${color}00`);
        if (data.length) {
            ctx.beginPath();
            data.forEach((row, index) => {
                const x = pad.left + (data.length < 2 ? plotWidth / 2 : plotWidth * index / (data.length - 1));
                const y = pad.top + plotHeight - Number(row.count || 0) * plotHeight / maxValue;
                if (index === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
                points.push({ x, y, tooltip: `${row.label} · ${label}: ${Number(row.count || 0).toLocaleString()}` });
            });
            ctx.lineTo(points[points.length - 1].x, height - pad.bottom);
            ctx.lineTo(points[0].x, height - pad.bottom);
            ctx.closePath();
            ctx.fillStyle = gradient;
            ctx.fill();
            ctx.beginPath();
            points.forEach((point, index) => {
                if (index === 0) ctx.moveTo(point.x, point.y);
                else ctx.lineTo(point.x, point.y);
            });
            ctx.strokeStyle = color;
            ctx.lineWidth = 2.2;
            ctx.lineJoin = "round";
            ctx.stroke();
            points.forEach((point, index) => {
                if (index % Math.max(1, Math.floor(points.length / 8)) !== 0 && index !== points.length - 1) return;
                ctx.beginPath();
                ctx.arc(point.x, point.y, 2.5, 0, Math.PI * 2);
                ctx.fillStyle = color;
                ctx.fill();
            });
            ctx.fillStyle = palette.muted;
            ctx.textAlign = "left";
            ctx.fillText(data[0].label || "", pad.left, height - 9);
            ctx.textAlign = "right";
            ctx.fillText(data[data.length - 1].label || "", width - pad.right, height - 9);
        } else {
            ctx.fillStyle = palette.muted;
            ctx.textAlign = "center";
            ctx.fillText("No records in this window", width / 2, height / 2);
        }
        finishCanvas(canvas, layout, { points });
    };

    const drawBars = (canvas, data) => {
        const layout = prepareCanvas(canvas);
        if (!layout) return;
        const { ctx, width, height } = layout;
        const pad = { left: Math.min(145, width * .43), right: 27, top: 7, bottom: 5 };
        const rows = data.slice(0, 7);
        const rowHeight = rows.length ? (height - pad.top - pad.bottom) / rows.length : 0;
        const plotWidth = Math.max(25, width - pad.left - pad.right);
        const max = Math.max(1, ...rows.map((row) => Number(row.value || 0)));
        const points = [];
        ctx.font = "10px system-ui, sans-serif";
        ctx.textBaseline = "middle";
        rows.forEach((row, index) => {
            const y = pad.top + index * rowHeight;
            const barWidth = plotWidth * Number(row.value || 0) / max;
            ctx.textAlign = "right";
            ctx.fillStyle = palette.muted;
            const label = row.label.length > 23 ? `${row.label.slice(0, 22)}…` : row.label;
            ctx.fillText(label, pad.left - 8, y + rowHeight / 2);
            ctx.fillStyle = "rgba(118, 143, 161, .12)";
            ctx.fillRect(pad.left, y + rowHeight * .25, plotWidth, Math.max(5, rowHeight * .5));
            const gradient = ctx.createLinearGradient(pad.left, 0, pad.left + plotWidth, 0);
            gradient.addColorStop(0, palette.teal);
            gradient.addColorStop(1, palette.blue);
            ctx.fillStyle = gradient;
            ctx.fillRect(pad.left, y + rowHeight * .25, Math.max(2, barWidth), Math.max(5, rowHeight * .5));
            ctx.fillStyle = palette.muted;
            ctx.textAlign = "left";
            ctx.fillText(String(row.value), Math.min(width - pad.right + 3, pad.left + barWidth + 5), y + rowHeight / 2);
            points.push({ x: pad.left + Math.max(barWidth / 2, 2), tooltip: `${row.label}: ${row.value} analysis records` });
        });
        if (!rows.length) {
            ctx.fillStyle = palette.muted;
            ctx.textAlign = "center";
            ctx.fillText("No analysis types", width / 2, height / 2);
        }
        finishCanvas(canvas, layout, { points });
    };

    const drawRing = (canvas, compliant, failed) => {
        const layout = prepareCanvas(canvas);
        if (!layout) return;
        const { ctx, width, height } = layout;
        const x = width / 2;
        const y = height / 2;
        const outer = Math.min(width, height) * .42;
        const inner = outer * .72;
        const total = compliant + failed;
        ctx.beginPath();
        ctx.arc(x, y, outer, 0, Math.PI * 2);
        ctx.arc(x, y, inner, 0, Math.PI * 2, true);
        ctx.fillStyle = "rgba(117, 143, 160, .13)";
        ctx.fill("evenodd");
        const points = [];
        if (total > 0) {
            let angle = -Math.PI / 2;
            [[compliant, palette.green, "Complying"], [failed, palette.red, "Out of range"]].forEach(([value, color, label]) => {
                const sweep = Math.PI * 2 * value / total;
                const start = angle;
                const end = angle + sweep;
                ctx.beginPath();
                ctx.arc(x, y, outer, start, end);
                ctx.arc(x, y, inner, end, start, true);
                ctx.closePath();
                ctx.fillStyle = color;
                ctx.fill();
                points.push({ x, start: start + Math.PI / 2, end: end + Math.PI / 2, tooltip: `${label}: ${value.toLocaleString()}` });
                angle = end;
            });
        }
        finishCanvas(canvas, layout, { points, x, y, inner, outer, total });
        canvas.onpointermove = (event) => {
            const state = canvas._analysisChartState;
            const bounds = canvas.getBoundingClientRect();
            const px = event.clientX - bounds.left - state.x;
            const py = event.clientY - bounds.top - state.y;
            const radius = Math.sqrt(px * px + py * py);
            if (radius < state.inner || radius > state.outer) return showTooltip(canvas, null, "");
            let angle = Math.atan2(py, px) + Math.PI / 2;
            if (angle < 0) angle += Math.PI * 2;
            const segment = state.points.find((part) => {
                let start = part.start % (Math.PI * 2);
                let end = part.end % (Math.PI * 2);
                return start <= end ? angle >= start && angle <= end : angle >= start || angle <= end;
            });
            showTooltip(canvas, event, segment?.tooltip || "");
        };
    };

    const drawCharts = (activeRoot) => {
        const analysisSeries = readJson("analysis-daily-series");
        const resultSeries = readJson("result-daily-series");
        const types = readJson("analysis-type-series");
        const parameters = readJson("parameter-compliance-series");
        const readings = parameters.reduce((sum, item) => sum + Number(item.readings || 0), 0);
        const failed = parameters.reduce((sum, item) => sum + Number(item.failed || 0), 0);
        activeRoot.querySelectorAll("[data-analysis-chart]").forEach((canvas) => {
            const kind = canvas.dataset.analysisChart;
            if (kind === "analysis-trend") drawLine(canvas, analysisSeries, palette.teal, "analyses");
            if (kind === "result-trend") drawLine(canvas, resultSeries, palette.blue, "readings");
            if (kind === "analysis-types") drawBars(canvas, types);
            if (kind === "compliance-ring") drawRing(canvas, readings - failed, failed);
        });
    };

    const setTab = (activeRoot, name, updateUrl = true) => {
        const allowed = ["overview", "results", "compliance", "trends", "workflow", "models"];
        const selected = allowed.includes(name) ? name : "overview";
        activeRoot.dataset.activeTab = selected;
        activeRoot.querySelectorAll("[data-tab-target]").forEach((button) => {
            const isSelected = button.dataset.tabTarget === selected;
            button.setAttribute("aria-selected", String(isSelected));
            button.tabIndex = isSelected ? 0 : -1;
        });
        activeRoot.querySelectorAll("[data-tab-panel]").forEach((panel) => {
            panel.hidden = panel.dataset.tabPanel !== selected;
        });
        const hiddenInput = activeRoot.querySelector("[data-active-tab-input]");
        if (hiddenInput) hiddenInput.value = selected;
        if (updateUrl && window.history?.replaceState) {
            const url = new URL(window.location.href);
            if (selected === "overview") url.searchParams.delete("tab");
            else url.searchParams.set("tab", selected);
            window.history.replaceState({}, "", url);
        }
        window.requestAnimationFrame(() => drawCharts(activeRoot));
    };

    const applyCardFilters = (activeRoot) => {
        const query = (activeRoot.querySelector("[data-card-search]")?.value || "").trim().toLocaleLowerCase();
        const status = activeRoot.querySelector("[data-status-filter-select]")?.value || "all";
        const resultState = activeRoot.querySelector("[data-result-filter-select]")?.value || "all";
        let visible = 0;
        activeRoot.querySelectorAll("[data-analysis-cards]").forEach((container) => {
            const pinnedFilter = container.dataset.cardFilter || "all";
            container.querySelectorAll("[data-result-card]").forEach((card) => {
                const statusMatch = status === "all" || card.dataset.status === status;
                const resultMatch = resultState === "all" || card.dataset.resultState === resultState;
                const pinnedMatch = pinnedFilter === "all" || card.dataset.resultState === pinnedFilter;
                const textMatch = !query || (card.dataset.searchText || "").toLocaleLowerCase().includes(query);
                card.hidden = !(statusMatch && resultMatch && pinnedMatch && textMatch);
                if (!card.hidden) visible += 1;
            });
        });
        const count = activeRoot.querySelector(".analysis-card-count");
        if (count) count.textContent = `${visible} visible cards`;
    };

    const initialize = (activeRoot, preferredTab) => {
        const urlTab = new URL(window.location.href).searchParams.get("tab");
        const tab = preferredTab || urlTab || activeRoot.dataset.activeTab || "overview";
        setTab(activeRoot, tab, false);
        applyCardFilters(activeRoot);
    };

    const refreshDashboard = async () => {
        const activeRoot = root();
        if (!activeRoot || document.visibilityState === "hidden") return;
        const liveUrl = new URL(activeRoot.dataset.liveUrl, window.location.href);
        const pageUrl = new URL(window.location.href);
        liveUrl.search = pageUrl.search;
        liveUrl.searchParams.set("tab", activeRoot.dataset.activeTab || "overview");
        try {
            const response = await fetch(liveUrl, {
                headers: { "X-Requested-With": "XMLHttpRequest" },
                cache: "no-store",
                credentials: "same-origin",
            });
            if (!response.ok) return;
            const markup = await response.text();
            const parsed = new DOMParser().parseFromString(markup, "text/html");
            const replacement = parsed.querySelector("[data-analysis-dashboard]");
            const current = root();
            if (!replacement || !current) return;
            const preserved = {
                search: current.querySelector("[data-card-search]")?.value || "",
                status: current.querySelector("[data-status-filter-select]")?.value || "all",
                result: current.querySelector("[data-result-filter-select]")?.value || "all",
                tab: current.dataset.activeTab || "overview",
            };
            current.replaceWith(document.importNode(replacement, true));
            const next = root();
            if (!next) return;
            const search = next.querySelector("[data-card-search]");
            const status = next.querySelector("[data-status-filter-select]");
            const result = next.querySelector("[data-result-filter-select]");
            if (search) search.value = preserved.search;
            if (status) status.value = preserved.status;
            if (result) result.value = preserved.result;
            initialize(next, preserved.tab);
            next.classList.add("is-live-updated");
            window.setTimeout(() => next.classList.remove("is-live-updated"), 800);
        } catch {
            activeRoot.dataset.refreshError = "true";
        }
    };

    document.addEventListener("click", (event) => {
        const activeRoot = root();
        if (!activeRoot || !activeRoot.contains(event.target)) return;
        const tabButton = event.target.closest("[data-tab-target]");
        if (tabButton) {
            setTab(activeRoot, tabButton.dataset.tabTarget);
            return;
        }
        const openButton = event.target.closest("[data-open-tab]");
        if (openButton) {
            setTab(activeRoot, openButton.dataset.openTab);
            const filter = openButton.dataset.dashboardFilter || openButton.dataset.statusFilter;
            if (filter === "failed" || filter === "empty" || filter === "good") {
                const select = activeRoot.querySelector("[data-result-filter-select]");
                if (select) select.value = filter;
            } else if (filter) {
                const select = activeRoot.querySelector("[data-status-filter-select]");
                if (select) select.value = filter;
            }
            applyCardFilters(activeRoot);
        }
        const statusButton = event.target.closest("[data-status-filter]");
        if (statusButton && !openButton) {
            setTab(activeRoot, "results");
            const select = activeRoot.querySelector("[data-status-filter-select]");
            if (select) select.value = statusButton.dataset.statusFilter;
            applyCardFilters(activeRoot);
        }
        if (event.target.closest("[data-refresh-now]")) refreshDashboard();
    });

    document.addEventListener("keydown", (event) => {
        const activeRoot = root();
        if (!activeRoot) return;
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
            event.preventDefault();
            activeRoot.querySelector("[data-card-search]")?.focus();
            return;
        }
        if (!activeRoot.contains(event.target)) return;
        const tabButton = event.target.closest("[data-tab-target]");
        if (tabButton && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
            event.preventDefault();
            const buttons = [...activeRoot.querySelectorAll("[data-tab-target]")];
            const current = buttons.indexOf(tabButton);
            const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (current + (event.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length;
            buttons[next].focus();
            setTab(activeRoot, buttons[next].dataset.tabTarget);
        }
    });

    document.addEventListener("input", (event) => {
        const activeRoot = root();
        if (activeRoot && event.target.matches("[data-card-search]")) applyCardFilters(activeRoot);
    });
    document.addEventListener("change", (event) => {
        const activeRoot = root();
        if (activeRoot && event.target.matches("[data-status-filter-select], [data-result-filter-select]")) applyCardFilters(activeRoot);
    });

    let resizeTimer;
    window.addEventListener("resize", () => {
        window.clearTimeout(resizeTimer);
        resizeTimer = window.setTimeout(() => {
            const activeRoot = root();
            if (activeRoot) drawCharts(activeRoot);
        }, 100);
    }, { passive: true });

    const initialRoot = root();
    if (initialRoot) initialize(initialRoot);
    window.setInterval(refreshDashboard, 60000);
})();
