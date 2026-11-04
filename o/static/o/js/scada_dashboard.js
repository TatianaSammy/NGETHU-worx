(function () {
    "use strict";

    let activeDoseFilter = "all";
    let chartInstances = [];
    let activeTab = "overview";
    let sectionObserver = null;
    let waterQualityTrendChart = null;

    function parseJsonElement(id) {
        const node = document.getElementById(id);
        if (!node) return null;
        try { return JSON.parse(node.textContent || "{}"); }
        catch (error) {
            console.warn("Water SCADA dashboard data could not be read.", error);
            return null;
        }
    }

    function chartOptions(extra) {
        const defaults = {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 450 },
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: "rgba(5,17,30,.96)",
                    titleColor: "#eaf8ff",
                    bodyColor: "#bbd0dc",
                    borderColor: "rgba(82,210,225,.35)",
                    borderWidth: 1,
                    padding: 10,
                },
            },
            scales: {
                x: { grid: { color: "rgba(124,160,181,.09)" }, ticks: { color: "#718b9e", maxTicksLimit: 10, font: { size: 9 } } },
                y: { beginAtZero: true, grid: { color: "rgba(124,160,181,.1)" }, ticks: { color: "#718b9e", font: { size: 9 } } },
            },
        };
        const options = Object.assign({}, defaults, extra || {});
        options.plugins = Object.assign({}, defaults.plugins, (extra && extra.plugins) || {});
        options.scales = Object.assign({}, defaults.scales, (extra && extra.scales) || {});
        return options;
    }

    function makeChart(id, type, labels, datasets, options) {
        const canvas = document.getElementById(id);
        if (!canvas || !window.Chart) return null;
        const chart = new Chart(canvas.getContext("2d"), {
            type: type,
            data: { labels: labels || [], datasets: datasets || [] },
            options: chartOptions(options),
        });
        chartInstances.push(chart);
        return chart;
    }

    function renderCharts(data) {
        if (!window.Chart || !data) return;
        chartInstances.forEach(function (chart) { chart.destroy(); });
        chartInstances = [];

        if (data.labels && data.labels.length) {
            makeChart("processChart", "line", data.labels, [
                { label: "Recorded flow · m³/h", data: data.flow, yAxisID: "y", borderColor: "#51dceb", backgroundColor: "rgba(81,220,235,.12)", fill: true, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: .34 },
                { label: "Chemical usage · kg/day", data: data.usage, yAxisID: "y1", borderColor: "#ffbd63", backgroundColor: "rgba(255,189,99,.07)", fill: false, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 4, tension: .34 },
            ], chartOptions({
                plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 9 } } } },
                scales: {
                    x: { grid: { color: "rgba(124,160,181,.07)" }, ticks: { color: "#718b9e", maxTicksLimit: 10, font: { size: 9 } } },
                    y: { beginAtZero: true, position: "left", grid: { color: "rgba(124,160,181,.1)" }, ticks: { color: "#67cedb", font: { size: 9 } }, title: { display: true, text: "m³/h", color: "#67cedb", font: { size: 9 } } },
                    y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, ticks: { color: "#e5ad62", font: { size: 9 } }, title: { display: true, text: "kg/day", color: "#e5ad62", font: { size: 9 } } },
                },
            }));
            makeChart("flowStabilityChart", "line", data.labels, [
                { label: "Daily minimum · m³/h", data: data.flow_daily_min, borderColor: "rgba(112,154,178,.7)", backgroundColor: "rgba(112,154,178,.08)", borderWidth: 1, borderDash: [3, 4], pointRadius: 0, tension: .2, spanGaps: false },
                { label: "Daily maximum · m³/h", data: data.flow_daily_max, borderColor: "rgba(255,151,125,.7)", backgroundColor: "transparent", borderWidth: 1, borderDash: [3, 4], pointRadius: 0, tension: .2, spanGaps: false },
                { label: "Daily mean · m³/h", data: data.flow, borderColor: "#51dceb", backgroundColor: "rgba(81,220,235,.08)", borderWidth: 1.6, pointRadius: 1.5, tension: .3, spanGaps: false },
                { label: "7-day rolling mean · m³/h", data: data.flow_rolling_average, borderColor: "#ffc363", backgroundColor: "rgba(255,195,99,.08)", borderWidth: 2.4, pointRadius: 0, tension: .36, spanGaps: false },
            ], { plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 9 } } } } });
            makeChart("reservoirTrendChart", "line", data.labels, [
                { label: "Daily mean fill · %", data: data.reservoir_fill, borderColor: "#78bfff", backgroundColor: "rgba(120,191,255,.12)", fill: true, borderWidth: 2, pointRadius: 1.5, tension: .34, spanGaps: false },
                { label: "Low-level reference · 20%", data: data.reservoir_low_threshold, borderColor: "#ff8294", borderWidth: 1.5, borderDash: [6, 4], pointRadius: 0, fill: false },
            ], { plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 9 } } } }, scales: {
                y: { beginAtZero: true, max: 100, grid: { color: "rgba(124,160,181,.1)" }, ticks: { color: "#718b9e", callback: function (value) { return value + "%"; }, font: { size: 9 } } },
            } });
        }

        const waterQuality = data.water_quality || {};
        const qualitySelector = document.getElementById("waterQualityParameter");
        const qualityStageColors = { raw: "#57e0ed", coagulated: "#ffc363", clarified: "#b4a0ff", treated: "#4ee1ae" };
        function qualityDatasets(parameterId) {
            const parameterSeries = (waterQuality.series || {})[String(parameterId)] || {};
            const onlineSeries = (waterQuality.online_series || {})[String(parameterId)] || {};
            const projectionSeries = (waterQuality.projection_series || {})[String(parameterId)] || {};
            const limits = (waterQuality.limits || {})[String(parameterId)] || {};
            const datasets = (waterQuality.stages || []).reduce(function (items, stage) {
                const color = stage.color || qualityStageColors[stage.key] || "#57e0ed";
                items.push({
                    label: stage.label + " · QC",
                    data: parameterSeries[stage.key] || [],
                    borderColor: color,
                    backgroundColor: color + "24",
                    pointBackgroundColor: color,
                    borderWidth: 2,
                    pointRadius: 1.7,
                    pointHoverRadius: 4,
                    tension: .3,
                    fill: false,
                    spanGaps: false,
                });
                items.push({
                    label: stage.label + " · online",
                    data: onlineSeries[stage.key] || [],
                    borderColor: color,
                    backgroundColor: "transparent",
                    pointBackgroundColor: color,
                    borderWidth: 1.6,
                    borderDash: [6, 4],
                    pointRadius: 1.3,
                    pointHoverRadius: 4,
                    tension: .25,
                    fill: false,
                    spanGaps: false,
                });
                items.push({
                    label: stage.label + " · trend baseline",
                    data: projectionSeries[stage.key] || [],
                    borderColor: color,
                    backgroundColor: "transparent",
                    pointBackgroundColor: color,
                    borderWidth: 2,
                    borderDash: [2, 4],
                    pointRadius: 2,
                    pointHoverRadius: 4,
                    tension: .2,
                    fill: false,
                    spanGaps: false,
                });
                return items;
            }, []);
            (waterQuality.stages || []).forEach(function (stage) {
                const range = limits[stage.key] || {};
                const color = stage.color || qualityStageColors[stage.key] || "#57e0ed";
                ["minimum", "maximum"].forEach(function (bound) {
                    if (range[bound] === null || range[bound] === undefined) return;
                    datasets.push({
                        label: stage.label + " · " + bound + " limit",
                        data: (waterQuality.labels || []).map(function () { return Number(range[bound]); }),
                        borderColor: color + "99",
                        backgroundColor: "transparent",
                        borderWidth: 1,
                        borderDash: [5, 5],
                        pointRadius: 0,
                        pointHoverRadius: 0,
                        fill: false,
                        spanGaps: false,
                    });
                });
            });
            return datasets;
        }
        function renderQualityProjection(parameterId) {
            const parameter = (waterQuality.parameters || []).find(function (item) { return String(item.id) === String(parameterId); });
            const projectionByStage = (waterQuality.projections || {})[String(parameterId)] || {};
            const title = document.getElementById("waterQualityProjectionParameter");
            if (title) title.textContent = parameter ? parameter.name + " · " + parameter.unit : "Selected parameter";
            document.querySelectorAll("[data-projection-stage]").forEach(function (card) {
                const projection = projectionByStage[card.dataset.projectionStage] || {};
                [24, 72].forEach(function (hours) {
                    const horizon = hours === 24 ? "1" : "3";
                    const output = card.querySelector('[data-projection-value="' + hours + '"]');
                    const range = card.querySelector('[data-projection-range="' + hours + '"]');
                    const values = projection.horizons && projection.horizons[horizon];
                    if (output) output.textContent = projection.available && values ? Number(values.value).toFixed(3) : "—";
                    if (range) range.textContent = projection.available && values ? Number(values.low).toFixed(3) + " – " + Number(values.high).toFixed(3) + " " + (parameter ? parameter.unit : "") : "";
                });
                const meta = card.querySelector("[data-projection-meta]");
                if (meta) meta.textContent = projection.available
                    ? projection.direction + " · " + projection.observations + " daily QC points · as of " + projection.as_of
                    : projection.reason || (waterQuality.plant_required ? "Select one plant for isolated projections" : "Insufficient recent QC history");
            });
        }
        if (qualitySelector && waterQualityTrendChart === null) {
            const selectedParameter = (waterQuality.parameters || []).find(function (item) { return String(item.id) === qualitySelector.value; });
            waterQualityTrendChart = makeChart("waterQualityTrendChart", "line", waterQuality.labels || data.labels, qualityDatasets(qualitySelector.value), {
                plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 8 }, filter: function (item) { return item.text.indexOf("limit") === -1 && (item.text.indexOf("trend baseline") === -1 || item.text.indexOf("Raw water") === 0); } } } },
                scales: {
                    x: { grid: { color: "rgba(124,160,181,.07)" }, ticks: { color: "#718b9e", maxTicksLimit: 10, font: { size: 9 } } },
                    y: { beginAtZero: false, grid: { color: "rgba(124,160,181,.1)" }, ticks: { color: "#67cedb", font: { size: 9 } }, title: { display: true, text: selectedParameter ? selectedParameter.unit : "Measured value", color: "#67cedb", font: { size: 9 } } },
                },
            });
            renderQualityProjection(qualitySelector.value);
            if (qualitySelector && waterQualityTrendChart) {
                qualitySelector.addEventListener("change", function () {
                    const parameter = (waterQuality.parameters || []).find(function (item) { return String(item.id) === qualitySelector.value; });
                    waterQualityTrendChart.data.datasets = qualityDatasets(qualitySelector.value);
                    waterQualityTrendChart.options.scales.y.title.text = parameter ? parameter.unit : "Measured value";
                    waterQualityTrendChart.update();
                    renderQualityProjection(qualitySelector.value);
                });
            }
        } else if (qualitySelector) {
            renderQualityProjection(qualitySelector.value);
        }
        const stageCoverage = data.water_quality_stages || { labels: [], samples: [], compliance: [] };
        makeChart("waterQualityStageChart", "bar", stageCoverage.labels, [
            { label: "QC samples", data: stageCoverage.samples, yAxisID: "y", backgroundColor: ["rgba(87,224,237,.52)", "rgba(255,195,99,.52)", "rgba(180,160,255,.52)", "rgba(78,225,174,.52)"], borderColor: ["#57e0ed", "#ffc363", "#b4a0ff", "#4ee1ae"], borderWidth: 1, borderRadius: 4 },
            { type: "line", label: "Within configured limits · %", data: stageCoverage.compliance, yAxisID: "y1", borderColor: "#f0f4ff", backgroundColor: "#f0f4ff", pointRadius: 3, pointHoverRadius: 5, borderWidth: 2, spanGaps: false },
        ], { plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 9 } } } }, scales: {
            x: { grid: { display: false }, ticks: { color: "#91a8ba", font: { size: 8 } } },
            y: { beginAtZero: true, position: "left", grid: { color: "rgba(124,160,181,.08)" }, ticks: { color: "#718b9e", precision: 0, font: { size: 8 } }, title: { display: true, text: "Samples", color: "#718b9e", font: { size: 8 } } },
            y1: { beginAtZero: true, max: 100, position: "right", grid: { drawOnChartArea: false }, ticks: { color: "#c8d6e2", callback: function (value) { return value + "%"; }, font: { size: 8 } } },
        } });

        const pump = data.pump_status || {};
        const pumpLabels = ["Working", "Faulty", "Maintenance", "Decommissioned", "Unreported"];
        const pumpValues = [pump.working || 0, pump.faulty || 0, pump.maintenance || 0, pump.decommissioned || 0, pump.unknown || 0];
        const pumpColors = ["#4ee1ae", "#ff7085", "#ffc363", "#71879a", "#344c60"];
        makeChart("pumpChart", "doughnut", pumpLabels, [{ data: pumpValues, backgroundColor: pumpColors, borderColor: "#0b1a2b", borderWidth: 3, hoverOffset: 6 }], { cutout: "74%", plugins: { legend: { display: false } }, scales: {} });

        if (data.reservoirs && data.reservoirs.labels && data.reservoirs.labels.length) {
            makeChart("reservoirChart", "bar", data.reservoirs.labels, [{ label: "Latest recorded fill · %", data: data.reservoirs.data, backgroundColor: "rgba(72,207,218,.35)", borderColor: "#50d9e4", borderWidth: 1, borderRadius: 5 }], {
                indexAxis: "y", plugins: { legend: { display: false } }, scales: {
                    x: { beginAtZero: true, max: 100, grid: { color: "rgba(124,160,181,.09)" }, ticks: { color: "#718b9e", callback: function (value) { return value + "%"; }, font: { size: 9 } } },
                    y: { grid: { display: false }, ticks: { color: "#a4b9c9", font: { size: 9 } } },
                },
            });
        }
        if (data.stocks && data.stocks.labels && data.stocks.labels.length) {
            makeChart("stockChart", "bar", data.stocks.labels, [{ label: "Stock · kg", data: data.stocks.data, backgroundColor: "rgba(100,222,175,.32)", borderColor: "#62dfb2", borderWidth: 1, borderRadius: 4 }], { indexAxis: "y", plugins: { legend: { display: false } } });
        }

        function renderMicroLine(id, values, color, fill) {
            if (!values || !values.some(function (value) { return value !== null && value !== undefined; })) return;
            makeChart(id, "line", data.labels, [{ data: values, borderColor: color, backgroundColor: fill, borderWidth: 1.7, pointRadius: 0, pointHoverRadius: 2, tension: .42, fill: true, spanGaps: false }], {
                animation: { duration: 650 }, interaction: { mode: "nearest", intersect: false },
                plugins: { legend: { display: false }, tooltip: { enabled: false } },
                elements: { line: { capBezierPoints: true } },
                scales: { x: { display: false }, y: { display: false, beginAtZero: true } },
                layout: { padding: { top: 3, bottom: 2, left: 1, right: 1 } },
            });
        }
        renderMicroLine("overviewFlowMicro", data.flow, "#57e0ed", "rgba(87,224,237,.12)");
        renderMicroLine("overviewUsageMicro", data.usage, "#ffc36b", "rgba(255,195,107,.12)");
        renderMicroLine("overviewReservoirMicro", data.reservoir_fill, "#78bfff", "rgba(120,191,255,.12)");
        renderMicroLine("overviewComplianceMicro", data.compliance, "#c0a4ff", "rgba(192,164,255,.12)");
        const dosageTrends = data.established_dosage_trends || { datasets: [] };
        const dosageColors = ["#8db7ff", "#c2a5ff", "#65e7bb"];
        (dosageTrends.datasets || []).forEach(function (dose, index) {
            renderMicroLine("establishedDoseTrend" + (index + 1), dose.values, dosageColors[index % dosageColors.length], "rgba(103,183,215,.10)");
        });

        makeChart("overviewPumpMicro", "doughnut", pumpLabels, [{ data: pumpValues, backgroundColor: pumpColors, borderColor: "#0b1a2b", borderWidth: 2, hoverOffset: 3 }], { cutout: "76%", plugins: { legend: { display: false }, tooltip: { enabled: false } }, scales: {} });
        if (data.stocks && data.stocks.labels && data.stocks.labels.length) {
            makeChart("overviewStockMicro", "bar", data.stocks.labels, [{ data: data.stocks.data, backgroundColor: data.stocks.data.map(function (value) { return value < 50 ? "rgba(255,112,133,.62)" : "rgba(100,222,175,.45)"; }), borderWidth: 0, borderRadius: 2 }], {
                indexAxis: "y", plugins: { legend: { display: false }, tooltip: { enabled: false } },
                scales: { x: { display: false, beginAtZero: true }, y: { display: false, grid: { display: false } } },
                layout: { padding: { top: 1, bottom: 1, left: 1, right: 1 } }, categoryPercentage: .8, barPercentage: .75,
            });
        }

        const filterStatus = data.filter_status || { labels: [], data: [] };
        const filterPalette = ["#4ee1ae", "#69c8ff", "#ffc363", "#ff7085", "#a891ff", "#8da2b1"];
        makeChart("filterStatusChart", "doughnut", filterStatus.labels, [{ data: filterStatus.data, backgroundColor: filterStatus.labels.map(function (_label, index) { return filterPalette[index % filterPalette.length]; }), borderColor: "#0b1a2b", borderWidth: 3, hoverOffset: 5 }], { cutout: "72%", plugins: { legend: { display: false } }, scales: {} });
        if (data.filter_train && data.filter_train.labels && data.filter_train.labels.length) {
            makeChart("filterTrainChart", "bar", data.filter_train.labels, [
                { label: "Flow · m³/h", data: data.filter_train.flow, backgroundColor: "rgba(77,227,239,.52)", borderColor: "#4de3ef", borderWidth: 1, borderRadius: 4 },
                { label: "Design · m³/h", data: data.filter_train.capacity, backgroundColor: "rgba(120,150,255,.35)", borderColor: "#8798ff", borderWidth: 1, borderRadius: 4 },
            ], { plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 9 } } } } });
            makeChart("overviewFilterMicro", "bar", data.filter_train.labels, [
                { data: data.filter_train.flow, backgroundColor: "rgba(77,227,239,.72)", borderRadius: 2 },
                { data: data.filter_train.capacity, backgroundColor: "rgba(120,150,255,.38)", borderRadius: 2 },
            ], { plugins: { legend: { display: false }, tooltip: { enabled: false } }, scales: { x: { display: false }, y: { display: false, beginAtZero: true } }, layout: { padding: 2 } });
        }

        const valveStatus = data.valve_status || { labels: [], data: [] };
        const valveColors = ["#4ee1ae", "#ffc363", "#ff7085", "#69c8ff", "#b4a0ff", "#8da2b1"];
        makeChart("valveStatusChart", "doughnut", valveStatus.labels, [{ data: valveStatus.data, backgroundColor: valveStatus.labels.map(function (_label, index) { return valveColors[index % valveColors.length]; }), borderColor: "#0b1a2b", borderWidth: 3, hoverOffset: 5 }], { cutout: "72%", plugins: { legend: { display: false } }, scales: {} });
        makeChart("overviewValveMicro", "doughnut", valveStatus.labels, [{ data: valveStatus.data, backgroundColor: valveStatus.labels.map(function (_label, index) { return valveColors[index % valveColors.length]; }), borderColor: "#0b1a2b", borderWidth: 2, hoverOffset: 2 }], { cutout: "76%", plugins: { legend: { display: false }, tooltip: { enabled: false } }, scales: {} });

        if (data.clarifier_capacity && data.clarifier_capacity.labels && data.clarifier_capacity.labels.length) {
            makeChart("clarifierCapacityChart", "bar", data.clarifier_capacity.labels, [
                { label: "Design throughput · m³/h", data: data.clarifier_capacity.capacity, yAxisID: "y", backgroundColor: "rgba(77,227,239,.48)", borderColor: "#4de3ef", borderWidth: 1, borderRadius: 4 },
                { label: "Basin capacity · m³", data: data.clarifier_capacity.volume, yAxisID: "y1", backgroundColor: "rgba(184,143,255,.4)", borderColor: "#b48fff", borderWidth: 1, borderRadius: 4 },
            ], { plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 9 } } } }, scales: {
                y: { beginAtZero: true, position: "left", grid: { color: "rgba(124,160,181,.1)" }, ticks: { color: "#67d9e5", font: { size: 9 } }, title: { display: true, text: "m³/h", color: "#67d9e5", font: { size: 9 } } },
                y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, ticks: { color: "#b48fff", font: { size: 9 } }, title: { display: true, text: "m³", color: "#b48fff", font: { size: 9 } } },
            } });
        }

        const quality = data.dosing_quality || { labels: [], rates: [], records: [], compared: [] };
        if (quality.labels && quality.labels.length) {
            makeChart("dosingMixChart", "doughnut", data.categories.labels, [{ data: data.categories.data, backgroundColor: ["#76baff", "#b4a0ff", "#4ee1ae", "#ffc363"], borderColor: "#0b1a2b", borderWidth: 3, hoverOffset: 7 }], { cutout: "66%", plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 9 } } } }, scales: {} });
            makeChart("dosingAlignmentChart", "bar", quality.labels, [
                { label: "Within ±15% · %", data: quality.rates, yAxisID: "y", backgroundColor: "rgba(78,225,174,.48)", borderColor: "#4ee1ae", borderWidth: 1, borderRadius: 4 },
                { label: "Comparable runs", data: quality.compared || quality.records, yAxisID: "y1", backgroundColor: "rgba(120,150,255,.34)", borderColor: "#8798ff", borderWidth: 1, borderRadius: 4 },
            ], { plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 9 } } } }, scales: {
                y: { beginAtZero: true, max: 100, position: "left", grid: { color: "rgba(124,160,181,.1)" }, ticks: { color: "#67d9bd", callback: function (value) { return value + "%"; }, font: { size: 9 } } },
                y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, ticks: { color: "#a0a9ff", precision: 0, font: { size: 9 } } },
            } });
        }
        const phase = data.phase_performance || { labels: [], flow: [], capacity: [] };
        if (phase.labels && phase.labels.length) {
            makeChart("phasePerformanceChart", "bar", phase.labels, [
                { label: "Recorded flow · m³/h", data: phase.flow, backgroundColor: "rgba(77,227,239,.5)", borderColor: "#4de3ef", borderWidth: 1, borderRadius: 4 },
                { label: "Operational capacity · m³/h", data: phase.capacity, backgroundColor: "rgba(255,189,99,.36)", borderColor: "#ffbd63", borderWidth: 1, borderRadius: 4 },
            ], { plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 9 } } } } });
        }
        const treatment = data.treatment_capacity || { labels: [], filter_flow: [], clarifier_capacity: [] };
        if (treatment.labels && treatment.labels.length) {
            makeChart("treatmentCapacityChart", "bar", treatment.labels, [
                { label: "Filter flow · m³/h", data: treatment.filter_flow, backgroundColor: "rgba(77,227,239,.5)", borderColor: "#4de3ef", borderWidth: 1, borderRadius: 4 },
                { label: "Clarifier design · m³/h", data: treatment.clarifier_capacity, backgroundColor: "rgba(184,143,255,.42)", borderColor: "#b48fff", borderWidth: 1, borderRadius: 4 },
            ], { plugins: { legend: { display: true, position: "bottom", labels: { color: "#91a8ba", boxWidth: 8, usePointStyle: true, font: { size: 9 } } } } });
        }
        const coverage = data.inventory_coverage || { labels: [], days: [] };
        if (coverage.labels && coverage.labels.length) {
            makeChart("inventoryCoverageChart", "bar", coverage.labels, [{ label: "Projected coverage · days", data: coverage.days, backgroundColor: coverage.days.map(function (days) { return days !== null && days < 7 ? "rgba(255,112,133,.58)" : "rgba(78,225,174,.44)"; }), borderColor: "rgba(91,215,205,.62)", borderWidth: 1, borderRadius: 4 }], { indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, grid: { color: "rgba(124,160,181,.09)" }, ticks: { color: "#718b9e", font: { size: 9 } } }, y: { grid: { display: false }, ticks: { color: "#a4b9c9", font: { size: 9 } } } } });
        }
        const usageRanking = data.chemical_usage_rankings || { labels: [], quantities: [] };
        if (usageRanking.labels && usageRanking.labels.length) {
            makeChart("chemicalUsageRankingChart", "bar", usageRanking.labels, [{ label: "Recorded usage · kg", data: usageRanking.quantities, backgroundColor: usageRanking.quantities.map(function (_quantity, index) { return index < 3 ? "rgba(255,189,99,.64)" : "rgba(77,227,239,.38)"; }), borderColor: "rgba(91,215,205,.58)", borderWidth: 1, borderRadius: 4 }], { indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, grid: { color: "rgba(124,160,181,.09)" }, ticks: { color: "#718b9e", font: { size: 9 } } }, y: { grid: { display: false }, ticks: { color: "#a4b9c9", font: { size: 9 } } } } });
        }
    }

    function setActiveTab(name, focusTab) {
        const panel = document.querySelector('[data-tab-panel="' + name + '"]');
        const button = document.querySelector('[data-tab-target="' + name + '"]');
        if (!panel || !button) return;
        activeTab = name;
        document.querySelectorAll("[data-tab-target]").forEach(function (tab) {
            const active = tab === button;
            tab.classList.toggle("is-active", active);
            tab.setAttribute("aria-selected", active ? "true" : "false");
            tab.tabIndex = active ? 0 : -1;
        });
        document.querySelectorAll("[data-tab-panel]").forEach(function (item) {
            const active = item === panel;
            item.hidden = !active;
            item.classList.toggle("is-active", active);
        });
        const activeTabInput = document.getElementById("dashboardActiveTab");
        if (activeTabInput) activeTabInput.value = name;
        const currentUrl = new URL(window.location.href);
        currentUrl.searchParams.set("tab", name);
        window.history.replaceState(window.history.state, "", currentUrl.pathname + currentUrl.search + currentUrl.hash);
        const refreshLink = document.querySelector(".scada-refresh");
        if (refreshLink) {
            const refreshUrl = new URL(refreshLink.href, window.location.href);
            refreshUrl.searchParams.set("tab", name);
            refreshLink.href = refreshUrl.pathname + refreshUrl.search;
        }
        if (focusTab) button.focus();
        if (sectionObserver) sectionObserver.disconnect();
        observeActiveSections();
        const tabEntryTargets = {
            overview: "overview",
            process: "process-dynamics",
            dosing: "dosing-records",
            assets: "chemical-pumps",
            storage: "reservoirs",
            reference: "chemical-categories",
            reports: "analytics-reports",
        };
        updateSidebar(tabEntryTargets[name]);
        window.requestAnimationFrame(function () {
            chartInstances.forEach(function (chart) { chart.resize(); });
        });
    }

    function updateSidebar(targetId) {
        const links = Array.from(document.querySelectorAll(".scada-sidebar-link"));
        let matched = false;
        links.forEach(function (link) {
            let active = link.dataset.target === targetId;
            if (active && link.dataset.doseFilter) active = link.dataset.doseFilter === activeDoseFilter;
            if (active && !matched) {
                link.classList.add("scada-nav-active");
                matched = true;
            } else {
                link.classList.remove("scada-nav-active");
            }
        });
    }

    function observeActiveSections() {
        if (!("IntersectionObserver" in window)) return;
        const panel = document.querySelector('[data-tab-panel="' + activeTab + '"]');
        if (!panel) return;
        const ids = new Set(Array.from(document.querySelectorAll(".scada-sidebar-link")).map(function (link) { return link.dataset.target; }));
        const targets = Array.from(panel.querySelectorAll("[id]")).filter(function (element) { return ids.has(element.id); });
        sectionObserver = new IntersectionObserver(function (entries) {
            const visible = entries.filter(function (entry) { return entry.isIntersecting; }).sort(function (a, b) { return b.intersectionRatio - a.intersectionRatio; })[0];
            if (visible) updateSidebar(visible.target.id);
        }, { rootMargin: "-12% 0px -72% 0px", threshold: [0, .2, .5] });
        targets.forEach(function (target) { sectionObserver.observe(target); });
    }

    function initNavigation() {
        document.querySelectorAll("[data-tab-target]").forEach(function (button) {
            button.addEventListener("click", function () {
                const url = new URL(window.location.href);
                url.hash = "";
                window.history.replaceState(window.history.state, "", url.pathname + url.search);
                setActiveTab(button.dataset.tabTarget, false);
            });
            button.addEventListener("keydown", function (event) {
                const tabs = Array.from(document.querySelectorAll("[data-tab-target]"));
                const index = tabs.indexOf(button);
                let next = null;
                if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
                else if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
                else if (event.key === "Home") next = tabs[0];
                else if (event.key === "End") next = tabs[tabs.length - 1];
                if (next) { event.preventDefault(); setActiveTab(next.dataset.tabTarget, true); }
            });
        });

        document.querySelectorAll(".scada-sidebar-link").forEach(function (link) {
            link.addEventListener("click", function () {
                if (link.dataset.doseFilter) activeDoseFilter = link.dataset.doseFilter;
                const target = document.getElementById(link.dataset.target);
                const targetPanel = target && target.closest("[data-tab-panel]");
                if (targetPanel) setActiveTab(targetPanel.dataset.tabPanel, false);
                else if (link.dataset.target === "overview") setActiveTab("overview", false);
                if (target && target.tagName === "DETAILS") target.open = true;
                updateSidebar(link.dataset.target);
            });
        });
        document.getElementById("waterScadaDashboard").addEventListener("click", function (event) {
            const link = event.target.closest('a[href^="#"]');
            if (!link || link.classList.contains("scada-tab")) return;
            const targetId = (link.getAttribute("href") || "").slice(1);
            const target = targetId && document.getElementById(targetId);
            if (!target) return;
            const targetPanel = target.closest("[data-tab-panel]");
            if (targetPanel) setActiveTab(targetPanel.dataset.tabPanel, false);
            else if (targetId === "overview") setActiveTab("overview", false);
            if (target.tagName === "DETAILS") target.open = true;
            if (link.dataset.doseFilter) activeDoseFilter = link.dataset.doseFilter;
            window.requestAnimationFrame(function () {
                updateSidebar(targetId === "dosing-record-table" ? "dosing-records" : targetId);
                target.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
            });
        });
    }

    function initDosingFilters() {
        const controls = Array.from(document.querySelectorAll(".scada-dose-filter"));
        const rows = Array.from(document.querySelectorAll("#dosing-record-table tbody tr[data-dosing-type]"));
        controls.forEach(function (control) {
            control.addEventListener("click", function () {
                const selected = control.dataset.doseFilter;
                activeDoseFilter = selected;
                rows.forEach(function (row) { row.hidden = selected !== "all" && row.dataset.dosingType !== selected; });
                controls.forEach(function (item) { item.classList.toggle("scada-dose-filter--active", item === control); });
                linksAndDosingNav().forEach(function (item) { item.classList.toggle("scada-nav-active", item.dataset.doseFilter === selected); });
            });
        });
        const defaultControl = controls.find(function (control) { return control.dataset.doseFilter === "all"; });
        if (defaultControl) defaultControl.classList.add("scada-dose-filter--active");
    }

    function linksAndDosingNav() {
        return Array.from(document.querySelectorAll(".scada-sidebar-link[data-dose-filter]"));
    }

    function initReportActions() {
        const report = parseJsonElement("water-scada-report");
        document.querySelectorAll("[data-report-action]").forEach(function (button) {
            button.addEventListener("click", function () {
                if (button.dataset.reportAction === "print") {
                    window.print();
                    return;
                }
                if (button.dataset.reportAction === "download" && report) {
                    const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
                    const url = URL.createObjectURL(blob);
                    const anchor = document.createElement("a");
                    anchor.href = url;
                    anchor.download = "water-operations-report-" + (report.generated_on || "snapshot") + ".json";
                    document.body.appendChild(anchor);
                    anchor.click();
                    anchor.remove();
                    URL.revokeObjectURL(url);
                }
            });
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        const payload = parseJsonElement("water-scada-data");
        if (payload) renderCharts(payload);
        initNavigation();
        initDosingFilters();
        initReportActions();
        document.querySelectorAll(".scada-filters select").forEach(function (select) {
            select.addEventListener("keydown", function (event) {
                if (event.key === "Enter") select.form && select.form.requestSubmit();
            });
        });
        const hasTabParameter = new URLSearchParams(window.location.search).has("tab");
        let fragmentTarget = null;
        if (window.location.hash) {
            try { fragmentTarget = document.getElementById(decodeURIComponent(window.location.hash.slice(1))); }
            catch (_error) { fragmentTarget = null; }
        }
        const fragmentPanel = fragmentTarget && fragmentTarget.closest("[data-tab-panel]");
        const initialTab = hasTabParameter ? ((payload && payload.active_tab) || "overview") : (fragmentPanel ? fragmentPanel.dataset.tabPanel : "overview");
        setActiveTab(initialTab, false);
        if (fragmentPanel) window.requestAnimationFrame(function () { fragmentTarget.scrollIntoView({ block: "start" }); });
    });
})();
