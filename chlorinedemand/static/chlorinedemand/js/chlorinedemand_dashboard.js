/**
 * CHLORINE DEMAND ANALYTICAL BENCH & BREAKPOINT CURVE VISUALIZER
 * Interactive SVG curve plotting, incubation stopwatch, and real-time feed scaler.
 */

document.addEventListener("DOMContentLoaded", () => {
    initChlorineChart();
    initPlantFeedCalc();
    initKeyboardShortcuts();
    initChlorineReportAnalytics();
});

// ==============================================================================
// 1. DYNAMIC SVG BREAKPOINT CHLORINATION PLOTTER
// ==============================================================================
function getJarPointsFromDOM() {
    const points = [];
    for (let i = 1; i <= 6; i++) {
        const doseEl = document.getElementById(`input-dose-${i}`);
        const resEl = document.getElementById(`input-res-${i}`);
        const cardEl = document.getElementById(`jar-card-${i}`);
        if (doseEl && resEl) {
            const dose = parseFloat(doseEl.value) || 0;
            const res = parseFloat(resEl.value) || 0;
            const isOpt = cardEl ? cardEl.classList.contains("is-optimal-card") : false;
            points.push({ jar: i, dosage: dose, residual: res, is_optimal: isOpt });
        }
    }
    return points;
}

function getPlantFrcTarget() {
    const raw = document.querySelector(".cl-workspace-root")?.dataset?.targetResidual;
    if (raw === undefined || raw === null || raw.trim() === "") return null;
    const target = Number(raw);
    return Number.isFinite(target) && target >= 0 ? target : null;
}

function initChlorineChart() {
    renderChlorineCurve();
}

function renderChlorineCurve() {
    const svg = document.getElementById("breakpointCurveSvg");
    if (!svg) return;

    const points = getJarPointsFromDOM();
    if (points.length === 0) return;

    // ViewBox dimensions: 700 width, 280 height
    // Margin: Left 60, Right 670 (width=610), Top 25, Bottom 240 (height=215)
    const xMin = 60;
    const xMax = 670;
    const yMin = 25;
    const yMax = 240;

    // Find data bounds
    let maxDose = Math.max(...points.map(p => p.dosage), 4.0);
    if (maxDose <= 0) maxDose = 4.0;
    let maxRes = Math.max(...points.map(p => p.residual), 0.6);
    if (maxRes <= 0) maxRes = 0.6;
    const targetResidual = getPlantFrcTarget();
    maxRes = Math.max(maxRes, targetResidual ?? 0, 0.4);

    // Scale mappings
    const scaleX = (d) => xMin + ((d / maxDose) * (xMax - xMin));
    const scaleY = (r) => yMax - ((r / maxRes) * (yMax - yMin));

    const targetLine = document.getElementById("clTargetLine");
    const targetLabel = document.getElementById("clTargetLabel");
    if (targetLine && targetLabel && targetResidual !== null) {
        const targetY = scaleY(targetResidual);
        targetLine.setAttribute("y1", targetY);
        targetLine.setAttribute("y2", targetY);
        targetLine.removeAttribute("display");
        targetLabel.setAttribute("y", Math.max(yMin + 10, targetY - 5));
        targetLabel.textContent = `PLANT F.R.C. TARGET ${targetResidual.toFixed(3)} mg/L`;
        targetLabel.removeAttribute("display");
    } else {
        targetLine?.setAttribute("display", "none");
        targetLabel?.setAttribute("display", "none");
    }

    // Map screen points
    const mapped = points.map(p => ({
        x: scaleX(p.dosage),
        y: scaleY(p.residual),
        demandY: scaleY(Math.max(0, p.dosage - p.residual)),
        raw: p
    }));

    // Build SVG Path string using cubic Bezier interpolation
    let pathD = "";
    let areaD = "";
    if (mapped.length > 0) {
        pathD = `M ${mapped[0].x} ${mapped[0].y}`;
        areaD = `M ${mapped[0].x} ${yMax} L ${mapped[0].x} ${mapped[0].y}`;

        for (let i = 0; i < mapped.length - 1; i++) {
            const curr = mapped[i];
            const next = mapped[i + 1];
            const cpX1 = curr.x + (next.x - curr.x) / 2;
            const cpY1 = curr.y;
            const cpX2 = curr.x + (next.x - curr.x) / 2;
            const cpY2 = next.y;
            pathD += ` C ${cpX1} ${cpY1}, ${cpX2} ${cpY2}, ${next.x} ${next.y}`;
            areaD += ` C ${cpX1} ${cpY1}, ${cpX2} ${cpY2}, ${next.x} ${next.y}`;
        }
        areaD += ` L ${mapped[mapped.length - 1].x} ${yMax} Z`;
    }

    const pathEl = document.getElementById("clResidualPath");
    const areaEl = document.getElementById("clResidualArea");
    if (pathEl) pathEl.setAttribute("d", pathD);
    if (areaEl) areaEl.setAttribute("d", areaD);

    // Render interactive circles & markers
    const groupEl = document.getElementById("clPointsGroup");
    if (groupEl) {
        groupEl.innerHTML = "";
        mapped.forEach((pt) => {
            const g = document.createElementNS("http://www.w3.org/2000/svg", "g");

            if (pt.raw.is_optimal) {
                // Pulsing outer halo for optimal jar
                const halo = document.createElementNS("http://www.w3.org/2000/svg", "circle");
                halo.setAttribute("cx", pt.x);
                halo.setAttribute("cy", pt.y);
                halo.setAttribute("r", "12");
                halo.setAttribute("fill", "rgba(16, 185, 129, 0.3)");
                halo.setAttribute("stroke", "#10b981");
                halo.setAttribute("stroke-width", "1.5");
                g.appendChild(halo);
            }

            // Core dot
            const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
            dot.setAttribute("cx", pt.x);
            dot.setAttribute("cy", pt.y);
            dot.setAttribute("r", pt.raw.is_optimal ? "6" : "4.5");
            dot.setAttribute("fill", pt.raw.is_optimal ? "#10b981" : "#06b6d4");
            dot.setAttribute("stroke", "#0f172a");
            dot.setAttribute("stroke-width", "2");
            dot.style.cursor = "pointer";

            // Tooltip title
            const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
            title.textContent = `Jar ${pt.raw.jar}: Dose ${pt.raw.dosage} mg/L -> Residual ${pt.raw.residual} mg/L`;
            dot.appendChild(title);

            // Label above dot
            const txt = document.createElementNS("http://www.w3.org/2000/svg", "text");
            txt.setAttribute("x", pt.x);
            txt.setAttribute("y", pt.y - 10);
            txt.setAttribute("text-anchor", "middle");
            txt.setAttribute("fill", pt.raw.is_optimal ? "#34d399" : "#e2e8f0");
            txt.setAttribute("font-size", "10");
            txt.setAttribute("font-family", "JetBrains Mono");
            txt.setAttribute("font-weight", pt.raw.is_optimal ? "bold" : "normal");
            txt.textContent = `J${pt.raw.jar} (${pt.raw.residual})`;

            g.appendChild(dot);
            g.appendChild(txt);
            groupEl.appendChild(g);
        });
    }
}

// ==============================================================================
// 2. INTERACTIVE STEPPER & LIVE RECALCULATION
// ==============================================================================
window.stepChlorineDose = function(jarNum, delta) {
    const input = document.getElementById(`input-dose-${jarNum}`);
    if (input) {
        let val = parseFloat(input.value) || 0;
        val = Math.max(0, val + delta);
        input.value = val.toFixed(2);
        recalcChlorineJar(jarNum);
    }
};

window.stepChlorineResidual = function(jarNum, delta) {
    const input = document.getElementById(`input-res-${jarNum}`);
    if (input) {
        let val = parseFloat(input.value) || 0;
        val = Math.max(0, val + delta);
        input.value = val.toFixed(3);
        recalcChlorineJar(jarNum);
    }
};

window.recalcChlorineJar = function(jarNum) {
    const doseInput = document.getElementById(`input-dose-${jarNum}`);
    const resInput = document.getElementById(`input-res-${jarNum}`);
    const consumedEl = document.getElementById(`consumed-display-${jarNum}`);
    const retentionEl = document.getElementById(`retention-display-${jarNum}`);
    const gaugeEl = document.getElementById(`gauge-bar-${jarNum}`);
    const devEl = document.getElementById(`dev-display-${jarNum}`);
        const statusEl = document.getElementById(`status-badge-${jarNum}`);

    if (doseInput && resInput) {
        const dose = parseFloat(doseInput.value) || 0;
        const res = parseFloat(resInput.value) || 0;
        const consumed = Math.max(0, dose - res);
        const retention = dose > 0 ? (res / dose * 100) : 0;

        if (consumedEl) consumedEl.textContent = `${consumed.toFixed(2)} mg/L`;
        if (retentionEl) retentionEl.textContent = `${retention.toFixed(1)}%`;
        const targetResidual = getPlantFrcTarget();
        if (devEl) devEl.textContent = targetResidual === null
            ? "—"
            : `${Math.abs(res - targetResidual).toFixed(3)} mg/L`;

        // Update gauge bar width and color
        if (gaugeEl) {
            const gaugeRange = Math.max((targetResidual ?? 0) * 2, 0.4);
            const pct = Math.min(100, Math.max(0, (res / gaugeRange) * 100));
            gaugeEl.style.width = `${pct}%`;
            if (targetResidual === null) {
                gaugeEl.className = "progress-indicator ind-neutral";
            } else if (Math.abs(res - targetResidual) <= 0.05) {
                gaugeEl.className = "progress-indicator ind-green";
            } else {
                gaugeEl.className = "progress-indicator ind-amber";
            }
        }

        if (statusEl) {
            statusEl.classList.remove("status-optimal", "status-under", "status-over", "status-unconfigured");
            if (targetResidual === null) {
                statusEl.classList.add("status-unconfigured");
                statusEl.textContent = "Treated F.R.C. Limit Missing";
            } else if (Math.abs(res - targetResidual) <= 0.05) {
                statusEl.classList.add("status-optimal");
                statusEl.textContent = "Optimal Target";
            } else if (res < targetResidual) {
                statusEl.classList.add("status-under");
                statusEl.textContent = "Sub-Breakpoint";
            } else {
                statusEl.classList.add("status-over");
                statusEl.textContent = "Excess Residual";
            }
        }
    }

    renderChlorineCurve();
};

// ==============================================================================
// 3. CONTACT TIME INCUBATION STOPWATCH
// ==============================================================================
let timerInterval = null;
let timerSeconds = 10 * 60; // standard 10-minute contact time
let isTimerRunning = false;

window.toggleChlorineTimer = function() {
    const btn = document.getElementById("timerStartBtn");
    const badge = document.getElementById("timerPhaseBadge");

    if (isTimerRunning) {
        // Pause
        clearInterval(timerInterval);
        isTimerRunning = false;
        if (btn) btn.innerHTML = '<i class="fa-solid fa-play"></i> Resume';
        if (badge) { badge.textContent = "PAUSED"; badge.className = "badge bg-warning text-dark font-mono"; }
    } else {
        // Start
        isTimerRunning = true;
        if (btn) btn.innerHTML = '<i class="fa-solid fa-pause"></i> Pause';
        if (badge) { badge.textContent = "INCUBATING"; badge.className = "badge bg-emerald-subtle text-emerald font-mono"; }

        timerInterval = setInterval(() => {
            if (timerSeconds > 0) {
                timerSeconds--;
                updateTimerDisplay();
            } else {
                clearInterval(timerInterval);
                isTimerRunning = false;
                if (btn) btn.innerHTML = '<i class="fa-solid fa-play"></i> Start';
                if (badge) { badge.textContent = "COMPLETE"; badge.className = "badge bg-success text-white font-mono"; }
                playTimerChime();
            }
        }, 1000);
    }
};

window.resetChlorineTimer = function() {
    clearInterval(timerInterval);
    isTimerRunning = false;
    timerSeconds = 10 * 60;
    updateTimerDisplay();

    const btn = document.getElementById("timerStartBtn");
    const badge = document.getElementById("timerPhaseBadge");
    if (btn) btn.innerHTML = '<i class="fa-solid fa-play"></i> Start';
    if (badge) { badge.textContent = "STANDBY"; badge.className = "badge bg-dark border border-secondary text-light font-mono"; }

};

function updateTimerDisplay() {
    const display = document.getElementById("timerDisplay");
    if (!display) return;
    const m = Math.floor(timerSeconds / 60);
    const s = timerSeconds % 60;
    display.textContent = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function playTimerChime() {
    try {
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.frequency.setValueAtTime(880, audioCtx.currentTime); // A5
        gain.gain.setValueAtTime(0.2, audioCtx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 1.2);
        osc.start();
        osc.stop(audioCtx.currentTime + 1.2);
    } catch (e) {
        console.debug("Audio cue bypassed:", e);
    }
}

// ==============================================================================
// 4. PLANT CHEMICAL DOSING SCALER
// ==============================================================================
function initPlantFeedCalc() {
    calculatePlantFeed();
}

window.calculatePlantFeed = function() {
    const flowInput = document.getElementById("plantFlowInput");
    const doseInput = document.getElementById("calcDoseInput");

    const flow = parseFloat(flowInput ? flowInput.value : 1000) || 1000; // m³/hr
    const dose = parseFloat(doseInput ? doseInput.value : 2.0) || 2.0;    // mg/L (g/m³)

    // 1. Pure Chlorine Gas (100% active Cl₂)
    // kg/hr = (flow m³/hr * dose g/m³) / 1000
    const gasKgHr = (flow * dose) / 1000;
    const gasKgDay = gasKgHr * 24;

    // 2. Sodium Hypochlorite (10% w/v available Cl₂ = ~100 g/L available Cl₂)
    // L/hr = (gasKgHr * 1000 g/kg) / 100 g/L = gasKgHr * 10
    const hypoLitresHr = gasKgHr * 10;
    const hypoLitresDay = hypoLitresHr * 24;

    // 3. Calcium Hypochlorite (HTH 65% active Cl₂)
    // kg/day = gasKgDay / 0.65
    const hthKgDay = gasKgDay / 0.65;
    const hthDrumsDay = hthKgDay / 50; // assuming 50kg standard drums

    const gasHrEl = document.getElementById("feedGasKgHr");
    const gasDayEl = document.getElementById("feedGasKgDay");
    const hypoHrEl = document.getElementById("feedHypoLHr");
    const hypoDayEl = document.getElementById("feedHypoLDay");
    const hthDayEl = document.getElementById("feedHthKgDay");
    const hthBagsEl = document.getElementById("feedHthBags");

    if (gasHrEl) gasHrEl.textContent = `${gasKgHr.toFixed(2)} kg/hr`;
    if (gasDayEl) gasDayEl.textContent = `${gasKgDay.toFixed(1)} kg/day`;
    if (hypoHrEl) hypoHrEl.textContent = `${hypoLitresHr.toFixed(1)} L/hr`;
    if (hypoDayEl) hypoDayEl.textContent = `${Math.round(hypoLitresDay)} L/day`;
    if (hthDayEl) hthDayEl.textContent = `${hthKgDay.toFixed(1)} kg/day`;
    if (hthBagsEl) hthBagsEl.textContent = `~ ${hthDrumsDay.toFixed(1)} drums/day`;
};

// ==============================================================================
// 5. KEYBOARD SHORTCUTS
// ==============================================================================
function initKeyboardShortcuts() {
    document.addEventListener("keydown", (e) => {
        // Ctrl+S to save
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
            e.preventDefault();
            const saveBtn = document.getElementById("saveChlorineBtn");
            if (saveBtn) saveBtn.click();
        }

        // Space to toggle timer (if not focused on inputs)
        if (e.code === "Space" && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
            e.preventDefault();
            toggleChlorineTimer();
        }
    });
}

// ============================================================================
// 6. FILTERED REPORT ANALYTICS
// ============================================================================
function initChlorineReportAnalytics() {
    const source = document.getElementById("cd-report-data");
    if (!source || !window.Plotly) return;
    let data;
    try { data = JSON.parse(source.textContent || "{}"); } catch (_) { return; }
    const layout = {
        autosize: true, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
        font: { family: "Inter, Segoe UI, sans-serif", size: 9, color: "#98abc5" },
        margin: { l: 44, r: 12, t: 9, b: 32 }, showlegend: true,
        legend: { orientation: "h", y: 1.16, font: { size: 8 } },
        xaxis: { gridcolor: "rgba(148,163,184,.10)", tickfont: { color: "#8295b1" } },
        yaxis: { gridcolor: "rgba(148,163,184,.10)", tickfont: { color: "#8295b1" }, rangemode: "tozero" },
        hoverlabel: { bgcolor: "#101b34", bordercolor: "#52658a", font: { color: "#e9f5ff", size: 10 } },
    };
    const config = { responsive: true, displaylogo: false, modeBarButtonsToRemove: ["lasso2d", "select2d"] };
    const median = (items) => {
        const sorted = items.filter(Number.isFinite).sort((a, b) => a - b);
        if (!sorted.length) return null;
        const middle = Math.floor(sorted.length / 2);
        return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
    };
    const control = (timeline, field, color, unit) => {
        const values = timeline.map((row) => row.value).filter(Number.isFinite);
        const center = median(values);
        const mad = median(values.map((value) => Math.abs(value - center)));
        const dates = timeline.map((row) => row.date);
        const traces = [{ x: dates, y: timeline.map((row) => row.value), type: "scatter", mode: "lines+markers", name: field, line: { color, width: 2 }, marker: { color, size: 6 }, hovertemplate: `%{x}<br>%{y:.3f} ${unit}<extra></extra>` }];
        if (dates.length && center !== null) {
            [["Median", center, "#a78bfa", "solid"], ["Upper 3 MAD", center + 3 * 1.4826 * mad, "#f59e0b", "dash"], ["Lower 3 MAD", Math.max(0, center - 3 * 1.4826 * mad), "#f59e0b", "dash"]].forEach(([name, value, shade, dash]) => traces.push({ x: dates, y: dates.map(() => value), type: "scatter", mode: "lines", name, line: { color: shade, width: 1, dash }, hovertemplate: `${name}: %{y:.3f} ${unit}<extra></extra>` }));
        }
        const fitted = field === "Established dose" ? data.dose_fit : data.residual_fit;
        if (fitted?.length) traces.push({ x: fitted.map((row) => row.date), y: fitted.map((row) => row.value), type: "scatter", mode: "lines", name: "Theil–Sen trend", line: { color: "#34d399", dash: "dot", width: 2 }, hovertemplate: `Robust trend: %{y:.3f} ${unit}<extra></extra>` });
        const chartLayout = { ...layout, xaxis: { ...layout.xaxis, type: "date", title: "Test date" }, yaxis: { ...layout.yaxis, title: unit } };
        if (!dates.length) chartLayout.annotations = [{ text: "No measured values match these filters", x: .5, y: .5, xref: "paper", yref: "paper", showarrow: false, font: { color: "#8193ae", size: 10 } }];
        return [traces, chartLayout];
    };
    const drawControl = (id, timeline, field, color, unit) => {
        const node = document.getElementById(id);
        if (!node) return;
        const [traces, chartLayout] = control(timeline || [], field, color, unit);
        window.Plotly.react(node, traces, chartLayout, config);
    };
    requestAnimationFrame(() => {
        drawControl("cd-report-dose-timeline", data.dose_timeline, "Established dose", "#34d399", "mg/L");
        const responseNode = document.getElementById("cd-report-response");
        if (responseNode) {
            const response = data.dose_residual || {};
            const traces = [{ x: (response.points || []).map((point) => point.x), y: (response.points || []).map((point) => point.y), customdata: (response.points || []).map((point) => point.date), type: "scatter", mode: "markers", name: "Optimal jar", marker: { size: 8, color: "#22d3ee", line: { color: "#cffafe", width: 1 }, opacity: .82 }, hovertemplate: "Dose %{x:.2f} mg/L<br>Residual %{y:.3f} mg/L<br>%{customdata}<extra></extra>" }];
            if (response.fit?.length) traces.push({ x: response.fit.map((point) => point.x), y: response.fit.map((point) => point.y), type: "scatter", mode: "lines", name: "Theil–Sen fit", line: { color: "#a78bfa", width: 2, dash: "dot" } });
            const targetResidual = getPlantFrcTarget();
            if (response.points?.length && targetResidual !== null) {
                const extent = response.points.reduce((range, point) => [Math.min(range[0], point.x), Math.max(range[1], point.x)], [Infinity, -Infinity]);
                traces.push({ x: extent, y: [targetResidual, targetResidual], type: "scatter", mode: "lines", name: `Plant F.R.C. target (${targetResidual.toFixed(3)} mg/L)`, line: { color: "#34d399", width: 1, dash: "dash" } });
            }
            const chartLayout = { ...layout, xaxis: { ...layout.xaxis, title: "Established dose (mg/L)" }, yaxis: { ...layout.yaxis, title: "Residual (mg/L)" } };
            if (!response.points?.length) chartLayout.annotations = [{ text: "No dose / residual pairs match these filters", x: .5, y: .5, xref: "paper", yref: "paper", showarrow: false, font: { color: "#8193ae", size: 10 } }];
            if (response.correlation) chartLayout.annotations = [{ text: `Spearman ρ ${response.correlation.rho} · p ${response.correlation.p ?? "—"}`, x: .99, y: .98, xref: "paper", yref: "paper", xanchor: "right", yanchor: "top", showarrow: false, font: { color: "#c4b5fd", size: 9 } }];
            window.Plotly.react(responseNode, traces, chartLayout, config);
        }
        drawHistogram("cd-report-dose-hist", data.dose_histogram, "Established dose", "#10b981", "mg/L");
        drawHistogram("cd-report-residual-hist", data.residual_histogram, "Residual chlorine", "#06b6d4", "mg/L");
    });
}
