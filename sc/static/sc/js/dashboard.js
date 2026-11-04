(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    document.querySelectorAll(".sc-roster-export[open]").forEach(function (menu) {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });

  document.addEventListener("DOMContentLoaded", function () {
    var dataNode = document.getElementById("sc-trend-data");
    var canvas = document.getElementById("scAttendanceTrend");
    if (!dataNode || !canvas || typeof Chart === "undefined") return;

    var rows = [];
    try { rows = JSON.parse(dataNode.textContent || "[]"); } catch (_) { return; }
    var ctx = canvas.getContext("2d");
    var gradient = ctx.createLinearGradient(0, 0, 0, 240);
    gradient.addColorStop(0, "rgba(21, 164, 190, .28)");
    gradient.addColorStop(1, "rgba(21, 164, 190, 0)");

    var chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: rows.map(function (row) { return row.label; }),
        datasets: [
          {
            label: "Rostered staff",
            data: rows.map(function (row) { return row.total; }),
            borderColor: "#159ebd",
            backgroundColor: gradient,
            pointBackgroundColor: "#fff",
            pointBorderColor: "#159ebd",
            pointBorderWidth: 2,
            pointRadius: 3,
            borderWidth: 2.5,
            fill: true,
            tension: .35
          },
          {
            label: "Present recorded to date",
            data: rows.map(function (row) { return row.present; }),
            borderColor: "#26ad7d",
            backgroundColor: "transparent",
            pointBackgroundColor: "#fff",
            pointBorderColor: "#26ad7d",
            pointBorderWidth: 2,
            pointRadius: 3,
            borderWidth: 2,
            fill: false,
            tension: .35
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { intersect: false, mode: "index" },
        plugins: {
          legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 7, color: "#75849a", font: { size: 10 } } },
          tooltip: { backgroundColor: "#122b45", padding: 10, displayColors: true }
        },
        scales: {
          x: { grid: { display: false }, ticks: { color: "#8694a6", font: { size: 10 } } },
          y: { beginAtZero: true, grid: { color: "rgba(117,132,154,.12)" }, ticks: { precision: 0, color: "#8694a6", font: { size: 10 } } }
        }
      }
    });
    function refreshTheme() {
      var style = getComputedStyle(document.documentElement);
      var muted = style.getPropertyValue("--theme-muted").trim() || "#75849a";
      var border = style.getPropertyValue("--theme-border").trim() || "rgba(117,132,154,.12)";
      chart.options.plugins.legend.labels.color = muted;
      chart.options.scales.x.ticks.color = muted;
      chart.options.scales.y.ticks.color = muted;
      chart.options.scales.y.grid.color = border;
      chart.update("none");
    }
    refreshTheme();
    window.addEventListener("themeChanged", refreshTheme);
  });
})();

(function () {
  "use strict";

  function cookie(name) {
    var value = document.cookie.split(";").map(function (part) { return part.trim(); }).find(function (part) { return part.indexOf(name + "=") === 0; });
    var token = value ? decodeURIComponent(value.slice(name.length + 1)) : "";
    var meta = document.querySelector('meta[name="csrf-token"]');
    return token || (meta ? meta.content : "");
  }

  function setOptions(select, items, label) {
    select.replaceChildren(new Option(label, ""));
    items.forEach(function (item) { select.add(new Option(item.label || item.name, item.value || item.id)); });
    select.disabled = items.length === 0;
  }

  function rosterActions() {
    var root = document.querySelector(".sc-roster-overview");
    if (!root) return;
    var dialog = document.getElementById("sc-roster-assignment-dialog");
    var form = document.getElementById("sc-roster-assignment-form");
    var toast = document.getElementById("sc-roster-toast");
    var dragged = null;
    var slots = [];
    var availabilityMessage = "";
    var lockedSectionId = "";
    var messageTimer;

    function notify(message, error) {
      if (!toast) return;
      toast.textContent = message;
      toast.dataset.state = error ? "error" : "success";
      toast.classList.add("is-visible");
      window.clearTimeout(messageTimer);
      messageTimer = window.setTimeout(function () { toast.classList.remove("is-visible"); }, 4200);
    }

    function selectedDateSlot() {
      return slots.find(function (slot) { return slot.date === document.getElementById("sc-roster-dialog-date").value; });
    }

    function updateSections() {
      var slot = selectedDateSlot();
      var sections = slot ? slot.sections : [];
      var sectionSelect = document.getElementById("sc-roster-dialog-section");
      setOptions(sectionSelect, sections, sections.length ? "Choose a section" : "No available section");
      var currentSection = sections.find(function (section) { return String(section.id) === lockedSectionId; });
      if (currentSection) {
        sectionSelect.value = String(currentSection.id);
        sectionSelect.disabled = true;
      } else if (sections.length === 1) {
        sectionSelect.value = String(sections[0].id);
        sectionSelect.disabled = true;
      }
      updateUsers();
    }

    function dateMatchesCategory(dateValue) {
      if (!dateValue) return true;
      var day = new Date(dateValue + "T12:00:00").getDay();
      var dayType = document.getElementById("sc-roster-dialog-day-type").value;
      if (dayType === "weekday") return day !== 0 && day !== 6;
      if (dayType === "weekend") return day === 0 || day === 6;
      return true;
    }

    function updateUsers() {
      var slot = selectedDateSlot();
      var dateInput = document.getElementById("sc-roster-dialog-date");
      var sectionId = document.getElementById("sc-roster-dialog-section").value;
      var section = slot && slot.sections.find(function (item) { return String(item.id) === sectionId; });
      var staffSelect = document.getElementById("sc-roster-dialog-staff");
      var staff = section ? section.users.map(function (person) { return { id: person.id, name: person.name }; }) : [];
      setOptions(staffSelect, staff, staff.length ? "Choose a staff member" : "No available staff");
      var dateMatches = dateMatchesCategory(dateInput.value);
      dateInput.setCustomValidity(dateMatches ? "" : "Choose a date that matches this shift category.");
      var statusMessage = !dateMatches
        ? "Choose a date that matches this shift category."
        : !dateInput.value
          ? "Choose a duty date within the selected week."
          : !slot
            ? availabilityMessage || "No eligible section is configured for this shift."
        : !section
          ? "Choose a section to see eligible staff."
          : staff.length
            ? staff.length + " staff available in the selected section."
            : availabilityMessage || "No eligible staff are available for this date.";
      document.getElementById("sc-roster-dialog-status").textContent = statusMessage;
      form.querySelector('[type="submit"]').disabled = staff.length === 0 || !dateInput.value || !dateMatches;
    }

    async function openAdd(zone) {
      if (!dialog) return;
      var status = document.getElementById("sc-roster-dialog-status");
      status.textContent = "Checking available staff…";
      document.getElementById("sc-roster-dialog-subtitle").textContent = zone.dataset.category + " · " + zone.dataset.weekStart + " to " + zone.dataset.weekEnd;
      document.getElementById("sc-roster-dialog-shift").value = zone.dataset.shiftId;
      document.getElementById("sc-roster-dialog-day-type").value = zone.dataset.dayType;
      document.getElementById("sc-roster-dialog-week-start").value = zone.dataset.weekStart;
      document.getElementById("sc-roster-dialog-week-end").value = zone.dataset.weekEnd;
      var dateInput = document.getElementById("sc-roster-dialog-date");
      dateInput.min = zone.dataset.weekStart;
      dateInput.max = zone.dataset.weekEnd;
      dateInput.value = "";
      lockedSectionId = "";
      try {
        var url = new URL(root.dataset.optionsUrl, window.location.origin);
        url.searchParams.set("shift_id", zone.dataset.shiftId);
        url.searchParams.set("week_start", zone.dataset.weekStart);
        url.searchParams.set("week_end", zone.dataset.weekEnd);
        url.searchParams.set("day_type", zone.dataset.dayType);
        var response = await fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } });
        var result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.message || "Unable to load roster candidates.");
        slots = result.slots || [];
        availabilityMessage = result.message || "";
        lockedSectionId = result.locked_section_id ? String(result.locked_section_id) : "";
        updateSections();
        dialog.showModal();
      } catch (error) {
        availabilityMessage = error.message;
        form.querySelector('[type="submit"]').disabled = true;
        status.textContent = error.message;
        dialog.showModal();
      }
    }

    function candidateDate(zone, sourceDate) {
      var start = new Date(zone.dataset.weekStart + "T12:00:00");
      var end = new Date(zone.dataset.weekEnd + "T12:00:00");
      var source = new Date(sourceDate + "T12:00:00");
      var dates = [];
      for (var current = new Date(start); current <= end; current.setDate(current.getDate() + 1)) {
        var day = current.getDay();
        if (zone.dataset.dayType === "weekday" && day !== 0 && day !== 6) dates.push(new Date(current));
        if (zone.dataset.dayType === "weekend" && (day === 0 || day === 6)) dates.push(new Date(current));
        if (zone.dataset.dayType === "holiday") dates.push(new Date(current));
      }
      if (!dates.length) return "";
      var preferred = dates.find(function (item) { return item.getDay() === source.getDay(); });
      var chosen = preferred || dates[0];
      return [chosen.getFullYear(), String(chosen.getMonth() + 1).padStart(2, "0"), String(chosen.getDate()).padStart(2, "0")].join("-");
    }

    async function postAction(payload) {
      var response = await fetch(root.dataset.actionUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken"), "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify(payload)
      });
      var result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.message || "The roster could not be updated.");
      return result;
    }

    root.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-open-roster-add]");
      if (trigger) openAdd(trigger.closest("[data-roster-dropzone]"));
    });
    root.addEventListener("contextmenu", function (event) {
      var zone = event.target.closest("[data-roster-dropzone]");
      if (!zone || !dialog) return;
      event.preventDefault();
      if (zone.dataset.hasDates === "false") return notify(zone.dataset.noDatesLabel || "No matching dates exist in this roster cell.", true);
      openAdd(zone);
    });

    root.addEventListener("dragstart", function (event) {
      var chip = event.target.closest("[data-assignment-id]");
      if (!chip || chip.getAttribute("draggable") !== "true") return;
      var sourceZone = chip.closest("[data-roster-dropzone]");
      dragged = { id: chip.dataset.assignmentId, date: chip.dataset.date, plantId: sourceZone && sourceZone.dataset.plantId };
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
      var zone = event.target.closest("[data-roster-dropzone]");
      if (!zone || !dragged || zone.dataset.hasDates === "false") return;
      event.preventDefault();
      zone.classList.add("is-drop-target");
    });
    root.addEventListener("dragleave", function (event) {
      var zone = event.target.closest("[data-roster-dropzone]");
      if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove("is-drop-target");
    });
    root.addEventListener("drop", async function (event) {
      var zone = event.target.closest("[data-roster-dropzone]");
      if (!zone || !dragged) return;
      event.preventDefault();
      zone.classList.remove("is-drop-target");
      if (zone.dataset.hasDates === "false") return notify(zone.dataset.noDatesLabel || "No matching dates exist in that roster cell.", true);
      if (dragged.plantId && dragged.plantId !== zone.dataset.plantId) return notify("Duty assignments cannot be moved between treatment plants.", true);
      var date = candidateDate(zone, dragged.date);
      if (!date) return notify("No matching dates exist in that roster cell.", true);
      try {
        await postAction({ action: "move", assignment_id: dragged.id, shift_id: zone.dataset.shiftId, date: date, day_type: zone.dataset.dayType, week_start: zone.dataset.weekStart, week_end: zone.dataset.weekEnd });
        notify("Duty assignment moved. Refreshing the roster…", false);
        window.setTimeout(function () { window.location.reload(); }, 500);
      } catch (error) { notify(error.message, true); }
    });

    if (form) {
      document.getElementById("sc-roster-dialog-date").addEventListener("change", updateSections);
      document.getElementById("sc-roster-dialog-section").addEventListener("change", updateUsers);
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        var submit = form.querySelector('[type="submit"]');
        submit.disabled = true;
        try {
          await postAction({
            action: "add",
            date: document.getElementById("sc-roster-dialog-date").value,
            shift_id: document.getElementById("sc-roster-dialog-shift").value,
            day_type: document.getElementById("sc-roster-dialog-day-type").value,
            week_start: document.getElementById("sc-roster-dialog-week-start").value,
            week_end: document.getElementById("sc-roster-dialog-week-end").value,
            section_id: document.getElementById("sc-roster-dialog-section").value,
            staff_id: document.getElementById("sc-roster-dialog-staff").value
          });
          dialog.close();
          notify("Staff member added to the shift. Refreshing the roster…", false);
          window.setTimeout(function () { window.location.reload(); }, 500);
        } catch (error) {
          document.getElementById("sc-roster-dialog-status").textContent = error.message;
        } finally { submit.disabled = false; }
      });
      dialog.addEventListener("click", function (event) { if (event.target === dialog || event.target.closest("[data-close-roster-dialog]")) dialog.close(); });
    }
  }

  function rosterControls() {
    var root = document.querySelector(".sc-roster-overview");
    if (!root) return;
    var search = document.getElementById("sc-roster-search-input");
    var filter = document.getElementById("sc-roster-status-filter");
    var compact = document.getElementById("sc-roster-density-toggle");
    var reset = document.getElementById("sc-roster-filter-reset");
    var clock = document.getElementById("sc-roster-clock");
    function apply() {
      var query = (search.value || "").trim().toLocaleLowerCase();
      root.querySelectorAll("[data-roster-row]").forEach(function (row) {
        var assigned = Number(row.dataset.assignmentCount || 0) > 0;
        var matchesState = filter.value === "all" || (filter.value === "assigned" && assigned) || (filter.value === "open" && !assigned);
        row.hidden = !matchesState || (query && !row.textContent.toLocaleLowerCase().includes(query));
      });
    }
    if (search) search.addEventListener("input", apply);
    if (filter) filter.addEventListener("change", apply);
    if (compact) compact.addEventListener("click", function () { root.classList.toggle("is-compact"); compact.setAttribute("aria-pressed", String(root.classList.contains("is-compact"))); });
    if (reset) reset.addEventListener("click", function () { if (search) search.value = ""; if (filter) filter.value = "all"; root.classList.remove("is-compact"); apply(); });
    function updateClock() { if (clock) clock.textContent = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date()); }
    updateClock();
    window.setInterval(updateClock, 1000);
  }

  document.addEventListener("DOMContentLoaded", function () { rosterActions(); rosterControls(); });
})();
