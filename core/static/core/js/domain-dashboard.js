(function () {
  "use strict";

  function readPayload() {
    var node = document.getElementById("domain-dashboard-chart-data");
    if (!node) return [];
    try { return JSON.parse(node.textContent || "[]"); } catch (_) { return []; }
  }

  function renderCharts() {
    if (!window.QADCockpit || typeof Chart === "undefined") return;
    readPayload().forEach(function (chart) {
      if (!chart || !document.getElementById(chart.id)) return;
      var color = chart.color || "cyan";
      if (chart.type === "doughnut") {
        QADCockpit.doughnut(chart.id, chart.labels || [], chart.values || [], chart.colors || [color, "emerald", "amber", "rose"]);
        return;
      }
      var values = chart.datasets || [{ label: chart.label || "Records", data: chart.values || [], color: color, fill: chart.type === "line" }];
      var datasets = values.map(function (item) { return QADCockpit.dataset(item.label, item.data, item.color || color, !!item.fill); });
      if (chart.type === "line") QADCockpit.line(chart.id, chart.labels || [], datasets);
      else if (chart.type === "horizontal") QADCockpit.horizontalBar(chart.id, chart.labels || [], chart.values || [], color);
      else QADCockpit.bar(chart.id, chart.labels || [], datasets);
    });
  }

  function setTab(id, updateAddress) {
    document.querySelectorAll("[data-domain-panel]").forEach(function (panel) {
      panel.hidden = panel.dataset.domainPanel !== id;
    });
    document.querySelectorAll("[data-domain-tab]").forEach(function (tab) {
      tab.setAttribute("aria-selected", tab.dataset.domainTab === id ? "true" : "false");
    });
    document.querySelectorAll("[data-dashboard-active-tab]").forEach(function (input) { input.value = id; });
    if (updateAddress) {
      var url = new URL(window.location.href);
      url.searchParams.set("tab", id);
      window.history.replaceState({}, "", url);
    }
    requestAnimationFrame(function () {
      document.querySelectorAll("[data-domain-panel='" + id + "'] canvas").forEach(function (canvas) {
        if (window.Chart) {
          var instance = Chart.getChart(canvas);
          if (instance) instance.resize();
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    renderCharts();
    document.querySelectorAll("[data-domain-tab]").forEach(function (tab) {
      tab.addEventListener("click", function () { setTab(tab.dataset.domainTab, true); });
    });
    document.querySelectorAll("[data-dashboard-open]").forEach(function (button) {
      button.addEventListener("click", function () { setTab(button.dataset.dashboardOpen, true); });
    });
    document.querySelectorAll("[data-dashboard-refresh]").forEach(function (button) {
      button.addEventListener("click", function () { window.location.reload(); });
    });
    var initial = new URL(window.location.href).searchParams.get("tab");
    if (initial && document.querySelector("[data-domain-panel='" + initial + "']")) setTab(initial, false);
  });
})();
