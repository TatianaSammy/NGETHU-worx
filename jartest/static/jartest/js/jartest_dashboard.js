/**
 * ==============================================================================
 * JAR TEST ANALYTICAL DASHBOARD — JAVASCRIPT CONTROLLER
 * Matching chlorinedemand & PHadjustment Cyber Laboratory Interaction
 * ==============================================================================
 */

(function () {
    "use strict";

    // Global timers registry
    window.jtTimers = window.jtTimers || {};

    // 1. Coagulant Dosage Stepper
    window.stepCoagulantDose = function (jarNumber, step) {
        const input = document.getElementById(`input-dose-${jarNumber}`);
        if (!input) return;

        let cur = parseFloat(input.value) || 0;
        cur = Math.max(0, Math.round((cur + step) * 100) / 100);
        input.value = cur.toFixed(2);
        playTone(660, 0.05);
        window.triggerRecalculation();
    };

    // 2. Floc Observation Stopwatch (Start / Pause)
    window.toggleFlocTimer = function (jarNumber) {
        const display = document.getElementById(`timer-display-${jarNumber}`);
        const hiddenSecs = document.getElementById(`timer-seconds-${jarNumber}`);
        const btn = document.getElementById(`btn-timer-toggle-${jarNumber}`);
        if (!display || !btn) return;

        if (!window.jtTimers[jarNumber]) {
            let initialSecs = parseInt(hiddenSecs?.value || "0", 10);
            if (isNaN(initialSecs)) initialSecs = 0;
            window.jtTimers[jarNumber] = {
                seconds: initialSecs,
                interval: null,
                running: false,
            };
        }

        const t = window.jtTimers[jarNumber];

        if (t.running) {
            // Pause
            clearInterval(t.interval);
            t.running = false;
            btn.innerHTML = '<i class="fa-solid fa-play"></i>';
            btn.classList.remove("running");
            playTone(520, 0.08);
        } else {
            // Start
            t.running = true;
            btn.innerHTML = '<i class="fa-solid fa-pause"></i>';
            btn.classList.add("running");
            playTone(880, 0.08);

            t.interval = setInterval(() => {
                t.seconds += 1;
                const m = Math.floor(t.seconds / 60);
                const s = t.seconds % 60;
                display.value = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
                if (hiddenSecs) hiddenSecs.value = t.seconds;
            }, 1000);
        }
    };

    // 3. Floc Observation Stopwatch (Reset)
    window.resetFlocTimer = function (jarNumber) {
        const display = document.getElementById(`timer-display-${jarNumber}`);
        const hiddenSecs = document.getElementById(`timer-seconds-${jarNumber}`);
        const btn = document.getElementById(`btn-timer-toggle-${jarNumber}`);
        if (!display) return;

        if (window.jtTimers[jarNumber]) {
            clearInterval(window.jtTimers[jarNumber].interval);
            window.jtTimers[jarNumber].running = false;
            window.jtTimers[jarNumber].seconds = 0;
        }

        display.value = "00:00";
        if (hiddenSecs) hiddenSecs.value = 0;
        if (btn) {
            btn.innerHTML = '<i class="fa-solid fa-play"></i>';
            btn.classList.remove("running");
        }
        playTone(400, 0.06);
    };

    // 4. Recalculate Jar Removal Percentages
    window.recalcJarParams = function (jarNumber) {
        const root = document.querySelector(".jt-workspace-root");
        const rawTurb = parseFloat(root?.dataset?.rawTurbidity || "48.5") || 48.5;
        const rawColor = parseFloat(root?.dataset?.rawColor || "120.0") || 120.0;

        const card = document.getElementById(`jar-card-${jarNumber}`);
        if (!card) return;

        const turbInput = card.querySelector('input[data-param-type="turbidity"]');
        const colorInput = card.querySelector('input[data-param-type="color"]');
        const turbRemText = document.getElementById(`turb-rem-${jarNumber}`);
        const colorRemText = document.getElementById(`color-rem-${jarNumber}`);
        const turbBar = document.getElementById(`turb-bar-${jarNumber}`);
        const colorBar = document.getElementById(`color-bar-${jarNumber}`);

        if (turbInput && turbRemText) {
            const val = parseFloat(turbInput.value);
            if (!isNaN(val) && rawTurb > 0) {
                const rem = Math.max(0, Math.min(100, ((rawTurb - val) / rawTurb) * 100));
                turbRemText.textContent = `${rem.toFixed(1)}%`;
                if (turbBar) turbBar.style.width = `${rem}%`;
            }
        }

        if (colorInput && colorRemText) {
            const val = parseFloat(colorInput.value);
            if (!isNaN(val) && rawColor > 0) {
                const rem = Math.max(0, Math.min(100, ((rawColor - val) / rawColor) * 100));
                colorRemText.textContent = `${rem.toFixed(1)}%`;
                if (colorBar) colorBar.style.width = `${rem}%`;
            }
        }

        window.triggerRecalculation();
    };

    // 5. SVG Breakpoint Curve Plotter
    window.renderCurveChart = function () {
        const svg = document.getElementById("coagulationCurveSvg");
        if (!svg) return;

        const dataPoints = [];
        for (let i = 1; i <= 6; i++) {
            const card = document.getElementById(`jar-card-${i}`);
            if (!card) continue;

            const doseInput = document.getElementById(`input-dose-${i}`);
            const turbInput = card.querySelector('input[data-param-type="turbidity"]');
            const colorInput = card.querySelector('input[data-param-type="color"]');
            const isOptimal = card.classList.contains("is-optimal-card");

            const dose = parseFloat(doseInput?.value || "0") || (i * 5);
            const turb = parseFloat(turbInput?.value || "0");
            const color = parseFloat(colorInput?.value || "0");

            dataPoints.push({
                jar: i,
                dosage: dose,
                turbidity: isNaN(turb) ? 0 : turb,
                color: isNaN(color) ? 0 : color,
                isOptimal: isOptimal,
            });
        }

        if (dataPoints.length === 0) return;

        const w = 350;
        const h = 170;
        const padX = 35;
        const padY = 25;

        const maxDosage = Math.max(...dataPoints.map((d) => d.dosage), 40) * 1.1;
        const maxTurb = Math.max(...dataPoints.map((d) => d.turbidity), 10) * 1.2;
        const maxColor = Math.max(...dataPoints.map((d) => d.color), 20) * 1.2;

        const scaleX = (val) => padX + (val / maxDosage) * (w - padX * 1.6);
        const scaleY = (val) => h - padY - (val / maxTurb) * (h - padY * 1.8);
        const scaleColorY = (val) => h - padY - (val / maxColor) * (h - padY * 1.8);

        dataPoints.sort((a, b) => a.dosage - b.dosage);

        let turbPath = "";
        let colorPath = "";
        dataPoints.forEach((pt, idx) => {
            const x = scaleX(pt.dosage);
            const yt = scaleY(pt.turbidity);
            const yc = scaleColorY(pt.color);
            turbPath += idx === 0 ? `M ${x} ${yt}` : ` L ${x} ${yt}`;
            colorPath += idx === 0 ? `M ${x} ${yc}` : ` L ${x} ${yc}`;
        });

        const gridHtml = `
            <line x1="${padX}" y1="${h - padY}" x2="${w - 15}" y2="${h - padY}" stroke="rgba(71,85,105,0.4)" stroke-width="1"/>
            <line x1="${padX}" y1="12" x2="${padX}" y2="${h - padY}" stroke="rgba(71,85,105,0.4)" stroke-width="1"/>
            <text x="${w - 15}" y="${h - padY + 14}" fill="#94a3b8" font-size="8" font-family="JetBrains Mono">mg/L</text>
            <text x="${padX - 4}" y="12" fill="#38bdf8" font-size="8" font-family="JetBrains Mono" text-anchor="end">NTU</text>
        `;

        const curveHtml = `
            <path d="${colorPath}" fill="none" stroke="#fbbf24" stroke-width="1.5" stroke-dasharray="3,3" opacity="0.75"/>
            <path d="${turbPath}" fill="none" stroke="#38bdf8" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
        `;

        let pointsHtml = "";
        dataPoints.forEach((pt) => {
            const cx = scaleX(pt.dosage);
            const cy = scaleY(pt.turbidity);

            if (pt.isOptimal) {
                pointsHtml += `
                    <line x1="${cx}" y1="12" x2="${cx}" y2="${h - padY}" stroke="#10b981" stroke-width="1.2" stroke-dasharray="3,3"/>
                    <circle cx="${cx}" cy="${cy}" r="6" fill="none" stroke="#10b981" stroke-width="2" opacity="0.6"/>
                    <circle cx="${cx}" cy="${cy}" r="3.5" fill="#10b981"/>
                    <text x="${cx}" y="${Math.max(16, cy - 8)}" fill="#6ee7b7" font-size="8" font-weight="bold" font-family="JetBrains Mono" text-anchor="middle">J${pt.jar} Opt</text>
                `;
            } else {
                pointsHtml += `
                    <circle cx="${cx}" cy="${cy}" r="3" fill="#080e1a" stroke="#38bdf8" stroke-width="1.8"/>
                `;
            }
        });

        svg.innerHTML = gridHtml + curveHtml + pointsHtml;
    };

    window.triggerRecalculation = function () {
        window.renderCurveChart();
    };

    // 6. Historical dosage analytics and estimate workflow
    const analyticsState = { payload: null, estimate: null, initialized: false, rendered: false };
    let reportPayload = null;
    let reportAnalyticsRendered = false;
    const plotLayout = {
        paper_bgcolor: "rgba(0,0,0,0)",
        plot_bgcolor: "rgba(0,0,0,0)",
        font: { family: "Inter, Segoe UI, sans-serif", size: 10, color: "#98abc5" },
        margin: { l: 48, r: 20, t: 17, b: 42 },
        xaxis: { gridcolor: "rgba(148,163,184,.10)", zerolinecolor: "rgba(148,163,184,.16)", tickfont: { color: "#8295b1" }, linecolor: "rgba(148,163,184,.16)" },
        yaxis: { gridcolor: "rgba(148,163,184,.10)", zerolinecolor: "rgba(148,163,184,.16)", tickfont: { color: "#8295b1" }, linecolor: "rgba(148,163,184,.16)" },
        showlegend: false,
        hoverlabel: { bgcolor: "#101b34", bordercolor: "#52658a", font: { color: "#e9f5ff", size: 11 } },
    };
    const plotConfig = { responsive: true, displaylogo: false, modeBarButtonsToRemove: ["lasso2d", "select2d"] };

    function readDosageAnalytics() {
        const source = document.getElementById("jt-dosage-history-data");
        if (!source) return null;
        try { return JSON.parse(source.textContent || "{}"); } catch (_) { return null; }
    }

    function readReportAnalytics() {
        const source = document.getElementById("jt-report-data");
        if (!source) return null;
        try { return JSON.parse(source.textContent || "{}"); } catch (_) { return null; }
    }

    function setupWorkspaceTabs() {
        document.querySelectorAll("[data-jt-tab]").forEach((button) => {
            if (button.dataset.bound === "true") return;
            button.dataset.bound = "true";
            button.addEventListener("click", () => activateWorkspaceTab(button.dataset.jtTab));
            button.addEventListener("keydown", (event) => {
                if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                event.preventDefault();
                const tabs = Array.from(document.querySelectorAll("[data-jt-tab]"));
                const current = tabs.indexOf(button);
                const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
                tabs[next]?.focus();
                activateWorkspaceTab(tabs[next]?.dataset.jtTab);
            });
        });
    }

    function activateWorkspaceTab(name) {
        if (!name) return;
        document.querySelectorAll("[data-jt-tab]").forEach((tab) => {
            const active = tab.dataset.jtTab === name;
            tab.classList.toggle("is-active", active);
            tab.setAttribute("aria-selected", active ? "true" : "false");
        });
        document.querySelectorAll("[data-jt-panel]").forEach((panel) => {
            const active = panel.dataset.jtPanel === name;
            panel.hidden = !active;
            panel.classList.toggle("is-active", active);
        });
        if (name === "intelligence") {
            requestAnimationFrame(() => {
                if (!analyticsState.payload) analyticsState.payload = readDosageAnalytics();
                if (!analyticsState.rendered) {
                    renderDosageAnalytics();
                    analyticsState.rendered = true;
                } else {
                    ["jt-history-chart", "jt-correlation-chart", "jt-dose-distribution-chart"].forEach((id) => window.Plotly?.Plots?.resize?.(document.getElementById(id)));
                }
            });
        }
        if (name === "reports") {
            requestAnimationFrame(() => {
                if (!reportAnalyticsRendered) {
                    renderReportAnalytics();
                    reportAnalyticsRendered = true;
                } else {
                    ["jt-report-control-chart", "jt-report-association-chart", "jt-report-distribution-chart", "jt-report-groups-chart"].forEach((id) => window.Plotly?.Plots?.resize?.(document.getElementById(id)));
                }
            });
        }
    }

    function renderReportAnalytics(parameter = null) {
        const data = reportPayload || readReportAnalytics();
        if (!data || !document.getElementById("jt-report-control-chart")) return;
        reportPayload = data;
        renderReportControlChart(data);
        renderReportAssociationChart(data, parameter || document.getElementById("jt-report-association-select")?.value || data.associations?.[0]?.parameter || "");
        renderReportDistributionChart(data);
        renderReportGroupsChart(data);
    }

    function renderReportControlChart(data) {
        const node = document.getElementById("jt-report-control-chart");
        if (!node || !window.Plotly) return;
        const points = data.timeline || [];
        const dates = points.map((row) => row.date);
        const traces = [{
            x: dates, y: points.map((row) => row.dose), type: "scatter", mode: "lines+markers",
            name: "Established dose", line: { color: "#38bdf8", width: 2 },
            marker: { color: "#a5f3fc", size: 7 },
            hovertemplate: "%{x}<br>%{y:.2f} mg/L<extra>Jar-test outcome</extra>",
        }];
        const limits = data.control || {};
        if (dates.length && Number.isFinite(Number(limits.center))) {
            [["Median", limits.center, "#a78bfa", "solid"], ["Upper robust limit", limits.upper, "#f59e0b", "dash"], ["Lower robust limit", limits.lower, "#f59e0b", "dash"]].forEach(([name, value, color, dash]) => {
                traces.push({ x: dates, y: dates.map(() => value), type: "scatter", mode: "lines", name, line: { color, width: 1, dash }, hovertemplate: `${name}: %{y:.2f} mg/L<extra></extra>` });
            });
        }
        const fitted = limits.fit || [];
        if (fitted.length) traces.push({ x: fitted.map((row) => row.date), y: fitted.map((row) => row.dose), type: "scatter", mode: "lines", name: "Theil–Sen trend", line: { color: "#34d399", width: 2, dash: "dot" }, hovertemplate: "Robust trend: %{y:.2f} mg/L<extra></extra>" });
        const layout = { ...plotLayout, margin: { l: 45, r: 15, t: 8, b: 36 }, xaxis: { ...plotLayout.xaxis, type: "date", title: "Test date" }, yaxis: { ...plotLayout.yaxis, title: "Dose (mg/L)", rangemode: "tozero" }, showlegend: true, legend: { orientation: "h", y: 1.18, font: { size: 8 } } };
        if (!points.length) layout.annotations = [{ text: "No established doses match these filters", x: .5, y: .5, xref: "paper", yref: "paper", showarrow: false, font: { color: "#8193ae", size: 10 } }];
        window.Plotly.react(node, traces, layout, plotConfig);
    }

    function renderReportAssociationChart(data, parameter) {
        const node = document.getElementById("jt-report-association-chart");
        if (!node || !window.Plotly) return;
        const item = (data.associations || []).find((row) => row.parameter === parameter);
        if (!item) {
            window.Plotly.react(node, [], { ...plotLayout, annotations: [{ text: "No raw-water measurements match these filters", x: .5, y: .5, xref: "paper", yref: "paper", showarrow: false, font: { color: "#8193ae", size: 10 } }] }, plotConfig);
            return;
        }
        const traces = [{ x: item.points.map((row) => row.x), y: item.points.map((row) => row.y), customdata: item.points.map((row) => row.date), type: "scatter", mode: "markers", name: "Jar-test outcomes", marker: { color: "#38bdf8", size: 8, opacity: .84, line: { color: "#c4b5fd", width: 1 } }, hovertemplate: "%{customdata}<br>Raw-water value: %{x:.3g}<br>Established dose: %{y:.2f} mg/L<extra></extra>" }];
        if (item.trend?.length) traces.push({ x: item.trend.map((row) => row.x), y: item.trend.map((row) => row.y), type: "scatter", mode: "lines", name: "Theil–Sen robust fit", line: { color: "#34d399", width: 2, dash: "dash" }, hovertemplate: "Robust fit: %{y:.2f} mg/L<extra></extra>" });
        const rho = item.correlation?.rho;
        const title = rho == null ? item.label : `${item.label} · Spearman ρ=${rho}${item.correlation.p == null ? "" : ` · p=${item.correlation.p}`}`;
        const xTitle = item.unit ? `${item.label} (${item.unit})` : item.label;
        window.Plotly.react(node, traces, { ...plotLayout, title: { text: title, x: 0.02, font: { size: 10, color: "#b8c8df" } }, margin: { l: 48, r: 14, t: 34, b: 40 }, xaxis: { ...plotLayout.xaxis, title: xTitle }, yaxis: { ...plotLayout.yaxis, title: "Established dose (mg/L)", rangemode: "tozero" }, showlegend: Boolean(item.trend?.length), legend: { orientation: "h", y: 1.2, font: { size: 8 } } }, plotConfig);
    }

    function renderReportDistributionChart(data) {
        const node = document.getElementById("jt-report-distribution-chart");
        if (!node || !window.Plotly) return;
        const histogram = data.histogram || { centers: [], counts: [] };
        const layout = { ...plotLayout, margin: { l: 42, r: 12, t: 8, b: 38 }, xaxis: { ...plotLayout.xaxis, title: "Established dose (mg/L)" }, yaxis: { ...plotLayout.yaxis, title: "Tests", rangemode: "tozero" }, bargap: .12 };
        if (!histogram.counts?.length) layout.annotations = [{ text: "No established doses to distribute", x: .5, y: .5, xref: "paper", yref: "paper", showarrow: false, font: { color: "#8193ae", size: 10 } }];
        window.Plotly.react(node, [{ x: histogram.centers || [], y: histogram.counts || [], type: "bar", name: "Jar tests", marker: { color: "#818cf8", line: { color: "#c4b5fd", width: 1 } }, hovertemplate: "Dose bin: %{x:.2f} mg/L<br>Tests: %{y}<extra></extra>" }], layout, plotConfig);
    }

    function renderReportGroupsChart(data) {
        const node = document.getElementById("jt-report-groups-chart");
        if (!node || !window.Plotly) return;
        const traces = (data.groups || []).map((group) => ({ y: group.doses, type: "box", name: group.name, boxpoints: "all", jitter: .35, pointpos: 0, marker: { color: "#a78bfa", size: 5 }, line: { color: "#38bdf8" }, hovertemplate: "%{y:.2f} mg/L<extra>%{fullData.name}</extra>" }));
        const layout = { ...plotLayout, margin: { l: 45, r: 12, t: 8, b: 52 }, xaxis: { ...plotLayout.xaxis, title: "Coagulant", tickangle: -20 }, yaxis: { ...plotLayout.yaxis, title: "Established dose (mg/L)", rangemode: "tozero" }, showlegend: false };
        if (!traces.length) layout.annotations = [{ text: "No coagulant groups have established doses", x: .5, y: .5, xref: "paper", yref: "paper", showarrow: false, font: { color: "#8193ae", size: 10 } }];
        window.Plotly.react(node, traces, layout, plotConfig);
    }

    function fieldName(parameterId, catalog) {
        const match = (catalog || []).find((item) => Number(item.id) === Number(parameterId));
        return match ? `${match.name}${match.unit ? ` (${match.unit})` : ""}` : `Parameter ${parameterId}`;
    }

    function renderDosageAnalytics() {
        const data = analyticsState.payload;
        if (!data || !document.getElementById("jt-history-chart")) return;
        renderHistoryChart(data);
        renderCorrelationChart(data);
        renderDistributionChart(data);
        renderHistoryTable(data);
        renderSummaryStats(data.summary || {});
    }

    function renderHistoryChart(data) {
        const node = document.getElementById("jt-history-chart");
        if (!node || !window.Plotly) return;
        const history = data.history || [];
        const dates = history.map((item) => item.date);
        const doses = history.map((item) => Number(item.dose));
        const traces = [{
            type: "scatter", mode: "markers", name: "Approved established dosage", x: dates, y: doses,
            marker: { size: 9, color: doses, colorscale: [[0, "#38bdf8"], [.52, "#818cf8"], [1, "#c084fc"]], showscale: true, colorbar: { title: "mg/L", thickness: 8, len: .7, tickfont: { color: "#8799b4", size: 8 } }, line: { color: "#dbeafe", width: .8 }, opacity: .88 },
            customdata: history.map((item) => item.qc_reference || "Jar-test raw-water snapshot"),
            hovertemplate: "<b>%{x}</b><br>Established dose: %{y:.2f} mg/L<br>Source: %{customdata}<extra></extra>",
        }];
        if (dates.length >= 3) {
            const epoch = dates.map((date) => new Date(date).getTime());
            const meanX = epoch.reduce((a, b) => a + b, 0) / epoch.length;
            const meanY = doses.reduce((a, b) => a + b, 0) / doses.length;
            const denom = epoch.reduce((sum, value) => sum + (value - meanX) ** 2, 0);
            const slope = denom ? epoch.reduce((sum, value, index) => sum + (value - meanX) * (doses[index] - meanY), 0) / denom : 0;
            traces.push({
                type: "scatter", mode: "lines", name: "Linear historical trend",
                x: [dates[0], dates[dates.length - 1]],
                y: [meanY + slope * (epoch[0] - meanX), meanY + slope * (epoch[epoch.length - 1] - meanX)],
                line: { color: "rgba(103,232,249,.65)", width: 1.5, dash: "dot" },
                hovertemplate: "Trend: %{y:.2f} mg/L<extra></extra>",
            });
        }
        const summary = data.summary || {};
        const shapes = [];
        if (summary.q1 != null && summary.q3 != null) shapes.push({ type: "rect", xref: "paper", x0: 0, x1: 1, y0: Number(summary.q1), y1: Number(summary.q3), fillcolor: "rgba(129,140,248,.075)", line: { width: 0 }, layer: "below" });
        if (summary.median != null) shapes.push({ type: "line", xref: "paper", x0: 0, x1: 1, y0: Number(summary.median), y1: Number(summary.median), line: { color: "rgba(196,181,253,.68)", width: 1, dash: "dash" } });
        if (analyticsState.estimate?.ok) {
            traces.push({
                type: "scatter", mode: "markers", name: "Current sample estimate", x: [new Date().toISOString().slice(0, 10)], y: [analyticsState.estimate.estimate],
                error_y: { type: "data", symmetric: false, array: [Math.max(0, analyticsState.estimate.interval.high - analyticsState.estimate.estimate)], arrayminus: [Math.max(0, analyticsState.estimate.estimate - analyticsState.estimate.interval.low)], color: "#f0abfc", thickness: 2, width: 7 },
                marker: { size: 15, color: "#f0abfc", symbol: "star", line: { color: "#fff", width: 1 } },
                hovertemplate: "<b>Model estimate</b><br>%{y:.2f} mg/L<extra></extra>",
            });
        }
        const layout = { ...plotLayout, xaxis: { ...plotLayout.xaxis, title: "Jar-test date", titlefont: { size: 9 }, type: "date" }, yaxis: { ...plotLayout.yaxis, title: "Established dosage (mg/L)", titlefont: { size: 9 }, rangemode: "tozero" }, shapes, showlegend: false };
        if (!history.length) layout.annotations = [{ text: "Approved historical jar tests will appear here", x: .5, y: .5, xref: "paper", yref: "paper", showarrow: false, font: { color: "#8193ae", size: 11 } }];
        Plotly.react(node, traces, layout, plotConfig);
    }

    function renderCorrelationChart(data) {
        const node = document.getElementById("jt-correlation-chart");
        if (!node || !window.Plotly) return;
        const correlations = (data.correlations || []).slice(0, 12).reverse();
        const trace = {
            type: "bar", orientation: "h", x: correlations.map((item) => item.rho), y: correlations.map((item) => item.label),
            marker: { color: correlations.map((item) => item.rho >= 0 ? "#45d4e7" : "#a78bfa"), line: { color: "rgba(224,242,254,.25)", width: .6 } },
            customdata: correlations.map((item) => [item.p_value, item.n]),
            hovertemplate: "%{y}<br>Spearman ρ = %{x:.3f}<br>p = %{customdata[0]} · n = %{customdata[1]}<extra></extra>",
        };
        const layout = { ...plotLayout, margin: { l: 125, r: 20, t: 12, b: 34 }, xaxis: { ...plotLayout.xaxis, title: "Spearman rank correlation", titlefont: { size: 9 }, range: [-1, 1], zeroline: true }, yaxis: { ...plotLayout.yaxis, automargin: true, tickfont: { color: "#9aacc5", size: 9 } }, bargap: .32 };
        if (!correlations.length) layout.annotations = [{ text: "More varying approved raw-water records are needed for correlations", x: .5, y: .5, xref: "paper", yref: "paper", showarrow: false, font: { color: "#8193ae", size: 10 } }];
        Plotly.react(node, [trace], layout, plotConfig);
    }

    function renderDistributionChart(data) {
        const node = document.getElementById("jt-dose-distribution-chart");
        if (!node || !window.Plotly) return;
        const doses = (data.history || []).map((item) => Number(item.dose)).filter(Number.isFinite);
        const minDose = doses.reduce((value, dose) => Math.min(value, dose), Infinity);
        const maxDose = doses.reduce((value, dose) => Math.max(value, dose), -Infinity);
        const traces = doses.length ? [
            { type: "histogram", x: doses, name: "Frequency", marker: { color: "rgba(56,189,248,.47)", line: { color: "#67e8f9", width: 1 } }, xbins: { size: Math.max(.5, ((maxDose - minDose) || 1) / 10) }, hovertemplate: "%{x:.2f} mg/L<br>%{y} tests<extra></extra>" },
            { type: "scatter", mode: "markers", x: doses, y: doses.map(() => 0), yaxis: "y2", marker: { color: "#c4b5fd", size: 7, opacity: .8, symbol: "line-ns-open" }, hovertemplate: "%{x:.2f} mg/L<extra>Established dosage</extra>" },
        ] : [];
        const summary = data.summary || {};
        const shapes = [];
        if (summary.q1 != null && summary.q3 != null) shapes.push({ type: "rect", xref: "x", yref: "paper", x0: Number(summary.q1), x1: Number(summary.q3), y0: 0, y1: 1, fillcolor: "rgba(167,139,250,.1)", line: { width: 0 }, layer: "below" });
        if (summary.median != null) shapes.push({ type: "line", xref: "x", yref: "paper", x0: Number(summary.median), x1: Number(summary.median), y0: 0, y1: 1, line: { color: "rgba(196,181,253,.85)", width: 1.4, dash: "dash" } });
        const layout = { ...plotLayout, margin: { l: 42, r: 18, t: 12, b: 40 }, xaxis: { ...plotLayout.xaxis, title: "Established dose (mg/L)", titlefont: { size: 9 } }, yaxis: { ...plotLayout.yaxis, title: "Test frequency", titlefont: { size: 9 }, rangemode: "tozero" }, yaxis2: { overlaying: "y", side: "right", showgrid: false, showticklabels: false, zeroline: false }, barmode: "overlay", showlegend: false, shapes };
        if (!doses.length) layout.annotations = [{ text: "No approved doses in this cohort yet", x: .5, y: .5, xref: "paper", yref: "paper", showarrow: false, font: { color: "#8193ae", size: 11 } }];
        Plotly.react(node, traces, layout, plotConfig);
    }

    function renderHistoryTable(data) {
        const body = document.getElementById("jt-history-table-body");
        if (!body) return;
        body.replaceChildren();
        const history = (data.history || []).slice(-12).reverse();
        if (!history.length) {
            const row = body.insertRow(); row.className = "jt-empty-row";
            const cell = row.insertCell(); cell.colSpan = 4; cell.textContent = "No completed, approved jar tests match this plant and selected coagulant yet.";
            return;
        }
        history.forEach((item) => {
            const row = body.insertRow();
            const date = row.insertCell(); date.textContent = item.date;
            const dose = row.insertCell(); const strong = document.createElement("b"); strong.textContent = `${Number(item.dose).toFixed(2)} mg/L`; dose.append(strong);
            const raw = row.insertCell(); raw.textContent = `${Object.keys(item.raw || {}).length} measured parameter${Object.keys(item.raw || {}).length === 1 ? "" : "s"}`;
            const qc = row.insertCell(); qc.textContent = item.qc_reference || "QC match unavailable"; qc.className = item.qc_reference ? "jt-qc-reference" : "jt-no-reference";
        });
    }

    function renderSummaryStats(summary) {
        const count = document.querySelector('[data-jt-stat="count"]');
        const typical = document.querySelector('[data-jt-stat="median"]');
        const range = document.querySelector('[data-jt-stat="range"]');
        const pill = document.getElementById("jt-history-count");
        const tabCount = document.querySelector(".jt-tab-count");
        if (count) count.textContent = summary.count ?? 0;
        if (pill) pill.textContent = summary.count ?? 0;
        if (tabCount) tabCount.textContent = summary.count ?? 0;
        if (typical) typical.textContent = summary.median == null ? "—" : Number(summary.median).toFixed(2);
        if (range) range.textContent = summary.q1 == null || summary.q3 == null ? "—" : `${Number(summary.q1).toFixed(2)}–${Number(summary.q3).toFixed(2)}`;
    }

    function showPredictionMessage(message, isError) {
        analyticsState.estimate = null;
        const result = document.getElementById("jt-result-content");
        const placeholder = document.getElementById("jt-result-placeholder");
        const messageNode = document.getElementById("jt-result-message");
        const heading = document.getElementById("jt-result-heading");
        if (result) result.hidden = true;
        if (placeholder) placeholder.hidden = true;
        if (messageNode) {
            messageNode.hidden = false;
            messageNode.textContent = message || "An estimate could not be generated.";
            messageNode.dataset.status = isError ? "error" : "notice";
        }
        if (heading) heading.textContent = isError ? "Review the input" : "More evidence needed";
    }

    function showPrediction(result) {
        const placeholder = document.getElementById("jt-result-placeholder");
        const content = document.getElementById("jt-result-content");
        const message = document.getElementById("jt-result-message");
        const heading = document.getElementById("jt-result-heading");
        if (placeholder) placeholder.hidden = true;
        if (message) message.hidden = true;
        if (!result.ok) {
            analyticsState.estimate = null;
            showPredictionMessage(result.message, false);
            return;
        }
        analyticsState.estimate = result;
        if (content) content.hidden = false;
        if (heading) heading.textContent = "History-based estimate";
        document.getElementById("jt-result-dose").textContent = Number(result.estimate).toFixed(2);
        document.getElementById("jt-result-interval").textContent = `${Number(result.interval.low).toFixed(2)} – ${Number(result.interval.high).toFixed(2)} mg/L`;
        const confidence = document.getElementById("jt-result-confidence"); confidence.textContent = result.confidence; confidence.dataset.confidence = result.confidence;
        document.getElementById("jt-result-model").textContent = result.model;
        document.getElementById("jt-result-mae").textContent = `${Number(result.validation.mae).toFixed(2)} mg/L`;
        document.getElementById("jt-result-count").textContent = `${result.sample_count} tests`;
        document.getElementById("jt-result-validation").textContent = result.validation.method;
        document.getElementById("jt-result-coverage").textContent = `${Math.round(Number(result.input_coverage || 0) * 100)}%`;
        const modelScores = document.getElementById("jt-model-score-list");
        modelScores.replaceChildren();
        const scoreRows = (result.validation.model_scores || []).map((item) => ({ ...item, selected: item.model === result.model }));
        scoreRows.push({ model: "Historical median baseline", mae: result.validation.median_baseline_mae, n: result.validation.validation_cases, selected: result.model.includes("median") });
        const maxScore = Math.max(0.01, ...scoreRows.map((item) => Number(item.mae) || 0));
        scoreRows.forEach((item) => {
            const row = document.createElement("div"); row.className = `jt-model-score${item.selected ? " is-selected" : ""}`;
            const label = document.createElement("span"); label.textContent = item.model;
            const track = document.createElement("span"); track.className = "jt-model-score-track";
            const bar = document.createElement("i"); bar.style.width = `${Math.max(3, Number(item.mae) / maxScore * 100)}%`; track.append(bar);
            const value = document.createElement("b"); value.textContent = `${Number(item.mae).toFixed(2)} mg/L`;
            row.append(label, track, value); modelScores.append(row);
        });
        const warning = document.getElementById("jt-domain-warning");
        warning.hidden = !(result.out_of_domain || []).length;
        warning.textContent = (result.out_of_domain || []).length ? `One or more inputs fall outside the historical range for this cohort (${result.out_of_domain.map((item) => `${item.name}: ${Number(item.value).toPrecision(4)}; historical ${Number(item.min).toPrecision(4)}–${Number(item.max).toPrecision(4)}`).join(" · ")}). Treat the estimate as low confidence and run a fresh jar test.` : "";
        const cases = document.getElementById("jt-nearest-cases"); cases.replaceChildren();
        (result.nearest_cases || []).forEach((item) => {
            const row = document.createElement("div"); row.className = "jt-nearest-case";
            const date = document.createElement("span"); date.textContent = `${item.date} · signature distance ${item.distance}`;
            const dose = document.createElement("b"); dose.textContent = `${Number(item.dose).toFixed(2)} mg/L`;
            row.append(date, dose); cases.append(row);
        });
        const seal = document.getElementById("jt-result-seal"); if (seal) seal.innerHTML = '<i class="fa-solid fa-sparkles"></i>';
    }

    function initPredictionForm() {
        const form = document.getElementById("jt-prediction-form");
        if (!form || form.dataset.bound === "true") return;
        form.dataset.bound = "true";
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const button = document.getElementById("jt-predict-button");
            const loader = document.getElementById("jt-predictor-loading");
            const resultCard = document.getElementById("jt-estimate-card");
            if (button) button.disabled = true;
            if (loader) loader.hidden = false;
            activateWorkspaceTab("intelligence");
            try {
                const response = await fetch(form.dataset.predictUrl || form.action, {
                    method: "POST", body: new FormData(form), credentials: "same-origin",
                    headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" },
                });
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.message || "The estimate request was rejected.");
                analyticsState.payload = payload;
                showPrediction(payload);
                renderDosageAnalytics();
            } catch (error) {
                showPredictionMessage(error.message || "The prediction service could not be reached. Please retry.", true);
                renderDosageAnalytics();
            } finally {
                if (loader) loader.hidden = true;
                if (button) button.disabled = false;
                resultCard?.scrollIntoView({ behavior: "smooth", block: "nearest" });
            }
        });
    }

    function initializeDosageIntelligence() {
        if (!analyticsState.payload) analyticsState.payload = readDosageAnalytics();
        if (!reportPayload) reportPayload = readReportAnalytics();
        setupWorkspaceTabs();
        initPredictionForm();
        const associationSelect = document.getElementById("jt-report-association-select");
        if (associationSelect && associationSelect.dataset.bound !== "true") {
            associationSelect.dataset.bound = "true";
            associationSelect.addEventListener("change", () => renderReportAnalytics(associationSelect.value));
        }
        if (!window.jtInitialTabActivated) {
            window.jtInitialTabActivated = true;
            const requestedTab = new URLSearchParams(window.location.search).get("tab");
            if (["bench", "intelligence", "reports"].includes(requestedTab)) activateWorkspaceTab(requestedTab);
        }
        if (!analyticsState.initialized) {
            analyticsState.initialized = true;
            window.addEventListener("resize", () => {
                ["jt-history-chart", "jt-correlation-chart", "jt-dose-distribution-chart", "jt-report-control-chart", "jt-report-association-chart", "jt-report-distribution-chart", "jt-report-groups-chart"].forEach((id) => {
                    const node = document.getElementById(id);
                    if (node && node.offsetParent) window.Plotly?.Plots?.resize?.(node);
                });
            });
        }
    }

    // 7. Keyboard Shortcuts
    function initKeyboardShortcuts() {
        if (window.jtKeyboardShortcutsBound) return;
        window.jtKeyboardShortcutsBound = true;
        document.addEventListener("keydown", (e) => {
            if (e.key === "s" && (e.ctrlKey || e.metaKey)) {
                if (e.target?.closest?.("#jt-prediction-form")) return;
                e.preventDefault();
                const saveBtn = document.getElementById("saveJarTestBtn") || document.getElementById("save-jartest-btn");
                if (saveBtn) {
                    saveBtn.click();
                    playTone(800, 0.1);
                }
                return;
            }

            if (e.code === "Space" && !["INPUT", "TEXTAREA", "SELECT", "BUTTON", "A"].includes(document.activeElement?.tagName)) {
                e.preventDefault();
                // Toggle timer for optimal jar or jar 1
                const optJarCard = document.querySelector(".is-optimal-card");
                const jarNum = optJarCard ? optJarCard.id.replace("jar-card-", "") : "1";
                window.toggleFlocTimer(jarNum);
            }
        });
    }

    // Audio synthesizer
    function playTone(freq, dur) {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = "sine";
            osc.frequency.setValueAtTime(freq, ctx.currentTime);
            gain.gain.setValueAtTime(0.04, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + dur);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + dur);
        } catch (_) {}
    }

    // Initialization
    function setupDashboard() {
        window.renderCurveChart();
        initializeDosageIntelligence();
        initKeyboardShortcuts();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", setupDashboard);
    } else {
        setupDashboard();
    }

    document.body.addEventListener("htmx:afterSwap", () => {
        setupDashboard();
    });

    document.body.addEventListener("htmx:afterRequest", (event) => {
        const source = event.detail?.elt;
        const form = source?.closest?.("#jar-bench-form") || source;
        if (form?.id === "jar-bench-form" && event.detail?.successful) {
            // Re-render the linked leaderboard, sample reference, and matrix
            // together after a save or review action.
            window.location.reload();
        }
    });
})();
