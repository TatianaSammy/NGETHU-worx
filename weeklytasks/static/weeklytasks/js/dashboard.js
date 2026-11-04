(function () {
  "use strict";

  function readData(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try { return JSON.parse(node.textContent || "{}"); } catch (_) { return null; }
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (typeof Chart === "undefined") return;
    var chartInstances = [];
    var trend = readData("weekly-trend-data");
    var trendCanvas = document.getElementById("weeklyAssignmentTrend");
    if (trend && trendCanvas) {
      var ctx = trendCanvas.getContext("2d");
      var gradient = ctx.createLinearGradient(0, 0, 0, 240);
      gradient.addColorStop(0, "rgba(83, 115, 220, .25)");
      gradient.addColorStop(1, "rgba(83, 115, 220, 0)");
      chartInstances.push(new Chart(ctx, {
        type: "bar",
        data: {
          labels: trend.map(function (row) { return row.label; }),
          datasets: [{
            label: "People assignments",
            data: trend.map(function (row) { return row.count; }),
            backgroundColor: gradient,
            borderColor: "#5279d9",
            borderWidth: 1.5,
            borderRadius: 7,
            maxBarThickness: 34
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: { backgroundColor: "#122b45", padding: 10 }
          },
          scales: {
            x: { grid: { display: false }, ticks: { color: "#8694a6", font: { size: 10 } } },
            y: { beginAtZero: true, grid: { color: "rgba(117,132,154,.12)" }, ticks: { precision: 0, color: "#8694a6", font: { size: 10 } } }
          }
        }
      }));
    }

    var shifts = readData("weekly-shift-data");
    var shiftCanvas = document.getElementById("weeklyShiftDistribution");
    if (shifts && shiftCanvas) {
      chartInstances.push(new Chart(shiftCanvas.getContext("2d"), {
        type: "doughnut",
        data: {
          labels: shifts.labels.length ? shifts.labels : ["No assignments"],
          datasets: [{
            data: shifts.values.length ? shifts.values : [1],
            backgroundColor: shifts.values.length ? ["#17a6bd", "#5683e1", "#28ae80", "#dba53a", "#806bc5", "#d76578"] : ["#e6edf3"],
            borderWidth: 0,
            hoverOffset: 5
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: "68%",
          plugins: {
            legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 8, padding: 13, color: "#75849a", font: { size: 10 } } },
            tooltip: { enabled: shifts.values.length > 0, backgroundColor: "#122b45", padding: 10 }
          }
        }
      }));
    }
    function applyChartTheme() {
      var style = getComputedStyle(document.documentElement);
      var muted = style.getPropertyValue("--theme-muted").trim() || "#75849a";
      var border = style.getPropertyValue("--theme-border").trim() || "rgba(117,132,154,.16)";
      chartInstances.forEach(function (chart) {
        if (chart.options.scales) Object.keys(chart.options.scales).forEach(function (axis) {
          if (chart.options.scales[axis].ticks) chart.options.scales[axis].ticks.color = muted;
          if (chart.options.scales[axis].grid && chart.options.scales[axis].grid.display !== false) chart.options.scales[axis].grid.color = border;
        });
        if (chart.options.plugins && chart.options.plugins.legend && chart.options.plugins.legend.labels) chart.options.plugins.legend.labels.color = muted;
        chart.update("none");
      });
    }
    applyChartTheme();
    window.addEventListener("themeChanged", applyChartTheme);
  });
})();

(function () {
  "use strict";

  function csrfToken() {
    var item = document.cookie.split(";").map(function (part) { return part.trim(); }).find(function (part) { return part.indexOf("csrftoken=") === 0; });
    var token = item ? decodeURIComponent(item.slice("csrftoken=".length)) : "";
    var meta = document.querySelector('meta[name="csrf-token"]');
    return token || (meta ? meta.content : "");
  }

  function fillSelect(select, options, placeholder) {
    select.replaceChildren(new Option(placeholder, ""));
    options.forEach(function (item) { select.add(new Option(item.label || item.name, item.value || item.id)); });
    select.disabled = options.length === 0;
  }

  function scheduleActions() {
    var root = document.querySelector(".weekly-schedule-dashboard");
    if (!root) return;
    var dialog = document.getElementById("weekly-assignment-dialog");
    var form = document.getElementById("weekly-assignment-form");
    var toast = document.getElementById("weekly-action-toast");
    var slots = [];
    var dragged = null;
    var toastTimer;

    function notify(message, isError) {
      if (!toast) return;
      toast.textContent = message;
      toast.dataset.state = isError ? "error" : "success";
      toast.classList.add("is-visible");
      window.clearTimeout(toastTimer);
      toastTimer = window.setTimeout(function () { toast.classList.remove("is-visible"); }, 4200);
    }

    function selectedSlot() {
      var select = document.getElementById("weekly-dialog-date");
      return slots.find(function (slot) { return slot.date === select.value; });
    }

    function updateUsers() {
      var slot = selectedSlot();
      var users = slot ? slot.users : [];
      fillSelect(document.getElementById("weekly-dialog-user"), users, users.length ? "Choose a staff member" : "No eligible staff");
      updateShifts();
    }

    function updateShifts() {
      var slot = selectedSlot();
      var userId = document.getElementById("weekly-dialog-user").value;
      var user = slot && slot.users.find(function (person) { return String(person.id) === userId; });
      var shifts = user ? user.shifts.map(function (shift) { return { id: shift.id, name: shift.name }; }) : [];
      var select = document.getElementById("weekly-dialog-shift");
      fillSelect(select, shifts, shifts.length ? "Select rostered shift (optional)" : "No rostered shift (optional)");
      select.disabled = false;
      document.getElementById("weekly-dialog-status").textContent = user ? (shifts.length ? shifts.length + " rostered shift(s) available." : "No shift assignment exists for this date; task can still be assigned within the selected section.") : "Choose an available staff member.";
    }

    async function openAdd(record) {
      if (!dialog) return;
      document.getElementById("weekly-dialog-status").textContent = "Checking available staff…";
      document.getElementById("weekly-dialog-task").value = record.dataset.taskId;
      document.getElementById("weekly-assignment-subtitle").textContent = record.dataset.sectionName + " · " + record.dataset.startDate + " to " + record.dataset.endDate;
      try {
        var url = new URL(root.dataset.optionsUrl, window.location.origin);
        url.searchParams.set("task_id", record.dataset.taskId);
        var response = await fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } });
        var result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.message || "Unable to load task candidates.");
        slots = result.slots || [];
        fillSelect(document.getElementById("weekly-dialog-date"), slots.map(function (slot) { return { value: slot.date, label: slot.label }; }), slots.length ? "Choose a date" : "No open dates in this task week");
        updateUsers();
        dialog.showModal();
      } catch (error) {
        document.getElementById("weekly-dialog-status").textContent = error.message;
        dialog.showModal();
      }
    }

    async function post(payload) {
      var response = await fetch(root.dataset.actionUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify(payload)
      });
      var result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || "The task schedule could not be updated.");
      return result;
    }

    function targetDate(record, sourceDate) {
      var start = new Date(record.dataset.startDate + "T12:00:00");
      var end = new Date(record.dataset.endDate + "T12:00:00");
      var source = new Date(sourceDate + "T12:00:00");
      var dates = [];
      for (var current = new Date(start); current <= end; current.setDate(current.getDate() + 1)) dates.push(new Date(current));
      var chosen = dates.find(function (item) { return item.getDay() === source.getDay(); }) || dates[0];
      if (!chosen) return "";
      return [chosen.getFullYear(), String(chosen.getMonth() + 1).padStart(2, "0"), String(chosen.getDate()).padStart(2, "0")].join("-");
    }

    root.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-open-weekly-add]");
      if (trigger) openAdd(trigger.closest("[data-weekly-dropzone]"));
    });
    root.addEventListener("contextmenu", function (event) {
      var record = event.target.closest("[data-weekly-dropzone]");
      if (!record || !dialog) return;
      event.preventDefault();
      openAdd(record);
    });
    root.addEventListener("dragstart", function (event) {
      var chip = event.target.closest("[data-assignment-id]");
      if (!chip || chip.getAttribute("draggable") !== "true") return;
      dragged = { id: chip.dataset.assignmentId, date: chip.dataset.date, plantId: chip.dataset.plantId, sectionId: chip.dataset.sectionId };
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", chip.dataset.assignmentId);
      chip.classList.add("is-dragging");
    });
    root.addEventListener("dragend", function (event) {
      var chip = event.target.closest("[data-assignment-id]");
      if (chip) chip.classList.remove("is-dragging");
      root.querySelectorAll(".is-drop-target").forEach(function (item) { item.classList.remove("is-drop-target"); });
      dragged = null;
    });
    root.addEventListener("dragover", function (event) {
      var record = event.target.closest("[data-weekly-dropzone]");
      if (!record || !dragged) return;
      event.preventDefault();
      record.classList.add("is-drop-target");
    });
    root.addEventListener("dragleave", function (event) {
      var record = event.target.closest("[data-weekly-dropzone]");
      if (record && !record.contains(event.relatedTarget)) record.classList.remove("is-drop-target");
    });
    root.addEventListener("drop", async function (event) {
      var record = event.target.closest("[data-weekly-dropzone]");
      if (!record || !dragged) return;
      event.preventDefault();
      record.classList.remove("is-drop-target");
      if (dragged.plantId !== record.dataset.plantId || dragged.sectionId !== record.dataset.sectionId) return notify("Weekly task assignments cannot be moved outside their treatment plant and section.", true);
      try {
        await post({ action: "move", assignment_id: dragged.id, task_id: record.dataset.taskId, date: targetDate(record, dragged.date), shift_id: "" });
        notify("Weekly task assignment moved. Refreshing the schedule…", false);
        window.setTimeout(function () { window.location.reload(); }, 500);
      } catch (error) { notify(error.message, true); }
    });

    if (form) {
      document.getElementById("weekly-dialog-date").addEventListener("change", updateUsers);
      document.getElementById("weekly-dialog-user").addEventListener("change", updateShifts);
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        var submit = form.querySelector('[type="submit"]');
        submit.disabled = true;
        try {
          await post({
            action: "add",
            task_id: document.getElementById("weekly-dialog-task").value,
            date: document.getElementById("weekly-dialog-date").value,
            user_id: document.getElementById("weekly-dialog-user").value,
            shift_id: document.getElementById("weekly-dialog-shift").value
          });
          dialog.close();
          notify("Staff member assigned to the task. Refreshing the schedule…", false);
          window.setTimeout(function () { window.location.reload(); }, 500);
        } catch (error) { document.getElementById("weekly-dialog-status").textContent = error.message; }
        finally { submit.disabled = false; }
      });
      dialog.addEventListener("click", function (event) { if (event.target === dialog || event.target.closest("[data-close-weekly-dialog]")) dialog.close(); });
    }
  }

  function scheduleControls() {
    var root = document.querySelector(".weekly-schedule-dashboard");
    if (!root) return;
    var search = document.getElementById("weekly-schedule-search");
    var filter = document.getElementById("weekly-schedule-filter");
    var density = document.getElementById("weekly-density-toggle");
    var reset = document.getElementById("weekly-filter-reset");
    var clock = document.getElementById("weekly-live-clock");
    function apply() {
      var query = (search.value || "").trim().toLocaleLowerCase();
      root.querySelectorAll("[data-weekly-row]").forEach(function (row) {
        var states = Array.from(row.querySelectorAll("[data-weekly-status]")).map(function (cell) { return cell.dataset.weeklyStatus; });
        var planned = states.filter(function (state) { return state !== "not-planned"; });
        var staffed = planned.length > 0 && planned.every(function (state) { return state === "staffed"; });
        var partial = states.indexOf("partial") >= 0 || (states.indexOf("staffed") >= 0 && states.indexOf("unassigned") >= 0);
        var open = !planned.length || states.indexOf("unassigned") >= 0;
        var matches = filter.value === "all" || (filter.value === "staffed" && staffed) || (filter.value === "partial" && partial) || (filter.value === "open" && open);
        row.hidden = !matches || (query && !row.textContent.toLocaleLowerCase().includes(query));
      });
    }
    search.addEventListener("input", apply);
    filter.addEventListener("change", apply);
    density.addEventListener("click", function () {
      var active = root.classList.toggle("is-compact");
      density.setAttribute("aria-pressed", String(active));
    });
    reset.addEventListener("click", function () { search.value = ""; filter.value = "all"; root.classList.remove("is-compact"); density.setAttribute("aria-pressed", "false"); apply(); });
    function updateClock() { clock.textContent = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date()); }
    updateClock();
    window.setInterval(updateClock, 1000);
  }

  document.addEventListener("DOMContentLoaded", function () { scheduleActions(); scheduleControls(); });
})();
