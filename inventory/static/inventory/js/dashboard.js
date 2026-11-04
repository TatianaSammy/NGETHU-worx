(() => {
    const root = document.querySelector("[data-inventory-dashboard]");
    if (!root) return;

    const chartData = (id) => {
        const node = document.getElementById(id);
        if (!node) return [];
        try {
            return JSON.parse(node.textContent || "[]");
        } catch {
            return [];
        }
    };

    const movements = chartData("inventory-movement-data");
    const allocation = chartData("inventory-allocation-data");
    const categories = chartData("inventory-category-data");
    const palette = {
        addition: "#26d9c2",
        removal: "#f1788e",
        adjustment: "#67a9ff",
        available: "#26d9c2",
        reserved: "#67a9ff",
        issued: "#f3b64b",
        grid: "rgba(122, 146, 165, .2)",
        text: "#8496a6",
    };

    const setTab = (name, updateUrl = true) => {
        const valid = ["overview", "stock", "movements", "assets", "supplies", "controls"];
        const tab = valid.includes(name) ? name : "overview";
        root.dataset.activeTab = tab;
        document.querySelectorAll("[data-tab-target]").forEach((button) => {
            const selected = button.dataset.tabTarget === tab;
            button.setAttribute("aria-selected", String(selected));
            button.tabIndex = selected ? 0 : -1;
        });
        document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
            panel.hidden = panel.dataset.tabPanel !== tab;
        });
        if (updateUrl && window.history?.replaceState) {
            const url = new URL(window.location.href);
            if (tab === "overview") url.searchParams.delete("tab");
            else url.searchParams.set("tab", tab);
            window.history.replaceState({}, "", url);
        }
        window.requestAnimationFrame(drawCharts);
    };

    const filterTable = (tableId) => {
        const table = document.getElementById(tableId);
        if (!table) return;
        const search = document.querySelector(`[data-table-search="${tableId}"]`);
        const riskSelect = document.querySelector(`[data-risk-select="${tableId}"]`);
        const query = (search?.value || "").trim().toLocaleLowerCase();
        const risk = riskSelect?.value || "all";
        table.querySelectorAll("tbody [data-search-row]").forEach((row) => {
            const textMatches = !query || (row.innerText || "").toLocaleLowerCase().includes(query);
            const risks = (row.dataset.risk || "").split(/\s+/);
            const riskMatches = risk === "all" || risks.includes(risk);
            row.hidden = !(textMatches && riskMatches);
        });
    };

    const showTooltip = (canvas, event, content) => {
        const tooltip = canvas.parentElement?.querySelector(".inventory-chart-tooltip");
        if (!tooltip) return;
        if (!content) {
            tooltip.hidden = true;
            return;
        }
        tooltip.textContent = content;
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
            const state = canvas._inventoryChartState;
            if (!state) return;
            if (state.kind === "donut") {
                const bounds = canvas.getBoundingClientRect();
                const x = event.clientX - bounds.left - state.cx;
                const y = event.clientY - bounds.top - state.cy;
                const radius = Math.sqrt(x * x + y * y);
                if (radius < state.innerRadius || radius > state.outerRadius) {
                    showTooltip(canvas, event, "");
                    return;
                }
                let angle = Math.atan2(y, x) + Math.PI / 2;
                if (angle < 0) angle += Math.PI * 2;
                const segment = state.segments.find((part) => angle >= part.start && angle <= part.end);
                showTooltip(canvas, event, segment ? `${segment.label}: ${segment.value.toLocaleString(undefined, { maximumFractionDigits: 2 })} recorded` : "");
                return;
            }
            if (!state.points?.length) return;
            const bounds = canvas.getBoundingClientRect();
            const pointerX = event.clientX - bounds.left;
            const pointerY = event.clientY - bounds.top;
            const point = state.points.reduce((nearest, candidate) => {
                const distance = state.kind === "bars" ? Math.abs(candidate.y - pointerY) : Math.abs(candidate.x - pointerX);
                const nearestDistance = state.kind === "bars" ? Math.abs(nearest.y - pointerY) : Math.abs(nearest.x - pointerX);
                return distance < nearestDistance ? candidate : nearest;
            }, state.points[0]);
            if (!point) return;
            showTooltip(canvas, event, point.tooltip);
        });
        canvas.addEventListener("pointerleave", () => showTooltip(canvas, null, ""));
    };

    const prepareCanvas = (canvas) => {
        const bounds = canvas.getBoundingClientRect();
        if (!bounds.width || !bounds.height) return null;
        const scale = Math.max(1, Math.min(window.devicePixelRatio || 1, 2));
        canvas.width = Math.round(bounds.width * scale);
        canvas.height = Math.round(bounds.height * scale);
        const context = canvas.getContext("2d");
        if (!context) return null;
        context.setTransform(scale, 0, 0, scale, 0, 0);
        context.clearRect(0, 0, bounds.width, bounds.height);
        attachTooltip(canvas);
        return { context, width: bounds.width, height: bounds.height };
    };

    const drawMovements = (canvas) => {
        const layout = prepareCanvas(canvas);
        if (!layout) return;
        const { context: ctx, width, height } = layout;
        const pad = { left: 32, right: 10, top: 12, bottom: 27 };
        const plotWidth = Math.max(20, width - pad.left - pad.right);
        const plotHeight = Math.max(20, height - pad.top - pad.bottom);
        const maxValue = Math.max(1, ...movements.flatMap((row) => [row.addition, row.removal, row.adjustment]));
        ctx.font = "10px system-ui, sans-serif";
        ctx.textBaseline = "middle";
        ctx.strokeStyle = palette.grid;
        ctx.fillStyle = palette.text;
        for (let i = 0; i <= 4; i += 1) {
            const y = pad.top + (plotHeight * i) / 4;
            ctx.beginPath();
            ctx.moveTo(pad.left, y);
            ctx.lineTo(width - pad.right, y);
            ctx.stroke();
            const value = Math.round(maxValue * (1 - i / 4));
            ctx.textAlign = "right";
            ctx.fillText(String(value), pad.left - 7, y);
        }
        const series = [
            { key: "addition", color: palette.addition },
            { key: "removal", color: palette.removal },
            { key: "adjustment", color: palette.adjustment },
        ];
        const points = [];
        series.forEach(({ key, color }) => {
            ctx.beginPath();
            ctx.strokeStyle = color;
            ctx.lineWidth = 2;
            ctx.lineJoin = "round";
            movements.forEach((row, index) => {
                const x = pad.left + (movements.length <= 1 ? plotWidth / 2 : (plotWidth * index) / (movements.length - 1));
                const y = pad.top + plotHeight - (Number(row[key] || 0) / maxValue) * plotHeight;
                if (index === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
                if (key === "addition") {
                    const values = series.map((entry) => `${entry.key}: ${Number(row[entry.key] || 0)}`);
                    points[index] = { x, tooltip: `${row.label} · ${values.join(" · ")}` };
                }
            });
            ctx.stroke();
        });
        if (movements.length) {
            ctx.fillStyle = palette.text;
            ctx.textAlign = "left";
            ctx.fillText(movements[0].label, pad.left, height - 9);
            ctx.textAlign = "right";
            ctx.fillText(movements[movements.length - 1].label, width - pad.right, height - 9);
        }
        canvas._inventoryChartState = { kind: "line", points };
    };

    const drawAllocation = (canvas) => {
        const layout = prepareCanvas(canvas);
        if (!layout) return;
        const { context: ctx, width, height } = layout;
        const centerX = width / 2;
        const centerY = height / 2;
        const outerRadius = Math.min(width, height) * .39;
        const innerRadius = outerRadius * .68;
        const total = allocation.reduce((sum, entry) => sum + Math.max(0, Number(entry.value || 0)), 0);
        ctx.beginPath();
        ctx.arc(centerX, centerY, outerRadius, 0, Math.PI * 2);
        ctx.arc(centerX, centerY, innerRadius, 0, Math.PI * 2, true);
        ctx.fillStyle = "rgba(126, 148, 164, .15)";
        ctx.fill("evenodd");
        const segments = [];
        if (total > 0) {
            let angle = -Math.PI / 2;
            allocation.forEach((entry) => {
                const value = Math.max(0, Number(entry.value || 0));
                const amount = (value / total) * Math.PI * 2;
                const start = angle;
                const end = angle + amount;
                ctx.beginPath();
                ctx.arc(centerX, centerY, outerRadius, start, end);
                ctx.arc(centerX, centerY, innerRadius, end, start, true);
                ctx.closePath();
                ctx.fillStyle = entry.color || palette.available;
                ctx.fill();
                const pointerStart = ((start + Math.PI / 2) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2);
                const pointerEnd = pointerStart + amount;
                if (pointerEnd <= Math.PI * 2) {
                    segments.push({ ...entry, start: pointerStart, end: pointerEnd });
                } else {
                    segments.push({ ...entry, start: pointerStart, end: Math.PI * 2 });
                    segments.push({ ...entry, start: 0, end: pointerEnd - Math.PI * 2 });
                }
                angle = end;
            });
        }
        ctx.fillStyle = getComputedStyle(root).getPropertyValue("--inv-text").trim() || "#182230";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.font = "700 11px system-ui, sans-serif";
        ctx.fillText(total ? "RECORDED" : "NO LEDGER", centerX, centerY - 8);
        ctx.font = "750 15px system-ui, sans-serif";
        ctx.fillText(total ? total.toLocaleString(undefined, { maximumFractionDigits: 0 }) : "—", centerX, centerY + 12);
        canvas._inventoryChartState = { kind: "donut", cx: centerX, cy: centerY, outerRadius, innerRadius, segments };
    };

    const drawCategories = (canvas) => {
        const layout = prepareCanvas(canvas);
        if (!layout) return;
        const { context: ctx, width, height } = layout;
        const rows = categories.slice(0, 8);
        if (!rows.length) {
            ctx.fillStyle = palette.text;
            ctx.font = "12px system-ui, sans-serif";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText("No category records", width / 2, height / 2);
            canvas._inventoryChartState = { kind: "bars", points: [] };
            return;
        }
        const pad = { left: Math.min(118, width * .38), right: 31, top: 8, bottom: 5 };
        const rowHeight = rows.length ? (height - pad.top - pad.bottom) / rows.length : 0;
        const plotWidth = Math.max(30, width - pad.left - pad.right);
        const maxValue = Math.max(1, ...rows.map((row) => Number(row.value || 0)));
        const points = [];
        ctx.font = "10px system-ui, sans-serif";
        ctx.textBaseline = "middle";
        rows.forEach((row, index) => {
            const y = pad.top + index * rowHeight;
            const barWidth = (Number(row.value || 0) / maxValue) * plotWidth;
            ctx.fillStyle = palette.text;
            ctx.textAlign = "right";
            const label = row.label.length > 18 ? `${row.label.slice(0, 17)}…` : row.label;
            ctx.fillText(label, pad.left - 9, y + rowHeight * .5);
            ctx.fillStyle = "rgba(126, 148, 164, .13)";
            ctx.fillRect(pad.left, y + rowHeight * .23, plotWidth, Math.max(5, rowHeight * .48));
            const gradient = ctx.createLinearGradient(pad.left, 0, pad.left + plotWidth, 0);
            gradient.addColorStop(0, "#18b8ad");
            gradient.addColorStop(1, "#68a8ff");
            ctx.fillStyle = gradient;
            ctx.fillRect(pad.left, y + rowHeight * .23, Math.max(2, barWidth), Math.max(5, rowHeight * .48));
            ctx.fillStyle = palette.text;
            ctx.textAlign = "left";
            ctx.fillText(String(row.value), Math.min(width - pad.right + 5, pad.left + barWidth + 5), y + rowHeight * .5);
            points.push({ y: y + rowHeight * .5, tooltip: `${row.label}: ${row.value} item records` });
        });
        canvas._inventoryChartState = { kind: "bars", points };
    };

    function drawCharts() {
        document.querySelectorAll("[data-inventory-chart]").forEach((canvas) => {
            const type = canvas.dataset.inventoryChart;
            if (type === "movements") drawMovements(canvas);
            if (type === "allocation") drawAllocation(canvas);
            if (type === "categories") drawCategories(canvas);
        });
    }

    document.querySelectorAll("[data-tab-target]").forEach((button) => {
        button.addEventListener("click", () => setTab(button.dataset.tabTarget));
        button.addEventListener("keydown", (event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const tabs = [...document.querySelectorAll("[data-tab-target]")];
            const current = tabs.indexOf(button);
            const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
            tabs[next].focus();
            setTab(tabs[next].dataset.tabTarget);
        });
    });

    document.querySelectorAll("[data-open-tab]").forEach((button) => {
        button.addEventListener("click", () => {
            setTab(button.dataset.openTab);
            const risk = button.dataset.riskFilter;
            if (risk) {
                const select = document.querySelector('[data-risk-select="stock-table"]');
                if (select) {
                    select.value = risk;
                    filterTable("stock-table");
                }
            }
        });
    });

    document.querySelectorAll("[data-table-search]").forEach((input) => {
        input.addEventListener("input", () => filterTable(input.dataset.tableSearch));
    });
    document.querySelectorAll("[data-risk-select]").forEach((select) => {
        select.addEventListener("change", () => filterTable(select.dataset.riskSelect));
    });
    document.querySelector("[data-refresh-dashboard]")?.addEventListener("click", () => window.location.reload());

    document.addEventListener("keydown", (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
            event.preventDefault();
            const activePanel = document.querySelector(`[data-tab-panel="${root.dataset.activeTab}"]`);
            activePanel?.querySelector("[data-table-search]")?.focus();
        }
    });

    let resizeTimer;
    window.addEventListener("resize", () => {
        window.clearTimeout(resizeTimer);
        resizeTimer = window.setTimeout(drawCharts, 100);
    }, { passive: true });

    setTab(root.dataset.activeTab, false);
})();
