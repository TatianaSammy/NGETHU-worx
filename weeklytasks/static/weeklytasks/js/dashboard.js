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
      if (!dialog || !record) return;
      var taskId = record.dataset.taskId;
      if (!taskId) {
        notify("Task plan is not initialized for this cell.", true);
        return;
      }
      document.getElementById("weekly-dialog-status").textContent = "Checking available staff…";
      document.getElementById("weekly-dialog-task").value = taskId;
      document.getElementById("weekly-assignment-subtitle").textContent = (record.dataset.templateName || record.dataset.sectionName || "Routine Task") + " · " + record.dataset.startDate + " to " + record.dataset.endDate;
      try {
        var url = new URL(root.dataset.optionsUrl, window.location.origin);
        url.searchParams.set("task_id", taskId);
        var response = await fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } });
        var result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.message || "Unable to load task candidates.");
        slots = result.slots || [];
        var dateSelect = document.getElementById("weekly-dialog-date");
        fillSelect(dateSelect, slots.map(function (slot) { return { value: slot.date, label: slot.label }; }), slots.length ? "Choose a date" : "No open dates in this task week");
        var destinationDate = record.dataset.destinationDate || record.dataset.startDate;
        var selectedSlot = slots.find(function (slot) { return slot.date === destinationDate; }) || slots[0];
        dateSelect.value = selectedSlot ? selectedSlot.date : "";
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
      if (dragged.plantId !== record.dataset.plantId) {
        return notify("Weekly task assignments cannot be moved outside their treatment plant.", true);
      }
      var targetTask = Array.from(record.querySelectorAll("[data-task-target-id]"))
        .find(function (item) { return item.dataset.taskTargetSection === dragged.sectionId; });
      var taskId = targetTask ? targetTask.dataset.taskTargetId : "";
      if (!taskId) {
        return notify("This task cell has no plan for the staff member's section.", true);
      }
      try {
        await post({ action: "move", assignment_id: dragged.id, task_id: taskId, date: targetDate(record, dragged.date), shift_id: "" });
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
    var shiftFilter = document.getElementById("weekly-shift-filter");
    var density = document.getElementById("weekly-density-toggle");
    var reset = document.getElementById("weekly-filter-reset");
    var filtersForm = root.querySelector(".sc-assignment-filters");
    var plantFilter = filtersForm && filtersForm.querySelector('[name="plant"]');
    var sectionFilter = filtersForm && filtersForm.querySelector('[name="filter_section"]');
    function submitFilters() {
      if (!filtersForm) return;
      if (typeof filtersForm.requestSubmit === "function") filtersForm.requestSubmit();
      else filtersForm.submit();
    }
    if (plantFilter && sectionFilter) {
      plantFilter.addEventListener("change", function () {
        sectionFilter.value = "";
        if (shiftFilter) shiftFilter.value = "";
        submitFilters();
      });
    }
    if (sectionFilter && shiftFilter && filtersForm.dataset.cascadeSection === "true") {
      sectionFilter.addEventListener("change", function () {
        shiftFilter.value = "";
        submitFilters();
      });
    }
    function apply() {
      var query = (search.value || "").trim().toLocaleLowerCase();
      var shiftId = shiftFilter ? shiftFilter.value : "";
      root.querySelectorAll("[data-weekly-row]").forEach(function (row) {
        var states = Array.from(row.querySelectorAll("[data-weekly-status]")).map(function (cell) { return cell.dataset.weeklyStatus; });
        var planned = states.filter(function (state) { return state !== "not-planned"; });
        var staffed = planned.length > 0 && planned.every(function (state) { return state === "staffed"; });
        var partial = states.indexOf("partial") >= 0 || (states.indexOf("staffed") >= 0 && states.indexOf("unassigned") >= 0);
        var open = !planned.length || states.indexOf("unassigned") >= 0;
        var statusValue = filter ? filter.value : "all";
        var matches = statusValue === "all" || (statusValue === "staffed" && staffed) || (statusValue === "partial" && partial) || (statusValue === "open" && open);
        row.hidden = !matches || (query && !row.textContent.toLocaleLowerCase().includes(query));
      });
      root.querySelectorAll(".weekly-task-cell").forEach(function (cell) {
        var assignees = Array.from(cell.querySelectorAll(".weekly-assignee"));
        var visibleCount = 0;
        assignees.forEach(function (assignee) {
          var assignmentShift = assignee.dataset.shiftId || "none";
          var visible = !shiftId || assignmentShift === shiftId;
          assignee.hidden = !visible;
          if (visible) visibleCount += 1;
        });
        var emptyMessage = cell.querySelector("[data-shift-filter-empty]");
        if (emptyMessage) emptyMessage.hidden = !shiftId || visibleCount > 0 || assignees.length === 0;
      });
    }
    if (search) search.addEventListener("input", apply);
    if (filter) filter.addEventListener("change", apply);
    if (shiftFilter) shiftFilter.addEventListener("change", apply);
    if (density) density.addEventListener("click", function () {
      var active = root.classList.toggle("is-compact");
      density.setAttribute("aria-pressed", String(active));
    });
    if (reset) reset.addEventListener("click", function () {
      var url = new URL(window.location.href);
      ["q", "shift", "task_status"].forEach(function (key) { url.searchParams.delete(key); });
      window.location.assign(url.toString());
    });
    apply();
  }

  document.addEventListener("DOMContentLoaded", function () { scheduleActions(); scheduleControls(); });
})();
