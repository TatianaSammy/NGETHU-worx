(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    document.querySelectorAll(".sc-assignment-export[open]").forEach(function (menu) {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });

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

  function cascadeFilters() {
    var form = document.querySelector(".sc-assignment-filters");
    if (!form) return;
    var plant = form.querySelector('[name="plant"]');
    var section = form.querySelector('[name="filter_section"]');
    var shift = form.querySelector('[name="shift"]');
    function submitFilters() {
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    }
    if (plant && section && shift) {
      plant.addEventListener("change", function () {
        section.value = "";
        shift.value = "";
        submitFilters();
      });
    }
    if (section && shift && form.dataset.cascadeSection === "true") {
      section.addEventListener("change", function () {
        shift.value = "";
        submitFilters();
      });
    }
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
    var preferredSectionId = "";
    var canSelectSection = root.dataset.superuser === "true";
    var canManageSupervisors = root.dataset.canManageSupervisors === "true";
    var canEditRoster = root.dataset.canEditRoster === "true";
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
      var previousSectionId = sectionSelect.value;
      setOptions(sectionSelect, sections, sections.length ? "Choose a section" : "No available section");
      var currentSection = sections.find(function (section) { return String(section.id) === lockedSectionId; });
      var preferredSection = sections.find(function (section) { return String(section.id) === preferredSectionId; });
      var previousSection = sections.find(function (section) { return String(section.id) === previousSectionId; });
      if (!canSelectSection && currentSection) {
        sectionSelect.value = String(currentSection.id);
        sectionSelect.disabled = true;
      } else if (preferredSection) {
        sectionSelect.value = String(preferredSection.id);
      } else if (previousSection) {
        sectionSelect.value = String(previousSection.id);
      } else if (sections.length === 1) {
        sectionSelect.value = String(sections[0].id);
      }
      if (canSelectSection && sections.length) sectionSelect.disabled = false;
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
            : section.capacity_message || availabilityMessage || "No eligible staff are available for this date.";
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
      lockedSectionId = canSelectSection ? "" : zone.dataset.sectionId || "";
      preferredSectionId = zone.dataset.sectionId || "";
      try {
        var url = new URL(root.dataset.optionsUrl, window.location.origin);
        url.searchParams.set("shift_id", zone.dataset.shiftId);
        url.searchParams.set("plant_id", zone.dataset.plantId);
        url.searchParams.set("week_start", zone.dataset.weekStart);
        url.searchParams.set("week_end", zone.dataset.weekEnd);
        url.searchParams.set("day_type", zone.dataset.dayType);
        if (canSelectSection) {
          if (preferredSectionId) url.searchParams.set("preferred_section_id", preferredSectionId);
        } else if (lockedSectionId) {
          url.searchParams.set("section_id", lockedSectionId);
        }
        var response = await fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } });
        var result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.message || "Unable to load roster candidates.");
        slots = result.slots || [];
        availabilityMessage = result.message || "";
        lockedSectionId = result.locked_section_id ? String(result.locked_section_id) : "";
        preferredSectionId = result.preferred_section_id ? String(result.preferred_section_id) : preferredSectionId;
        canSelectSection = Boolean(result.can_select_section);
        document.getElementById("sc-roster-dialog-plant").textContent = result.plant ? result.plant.name : "Plant unavailable";
        document.getElementById("sc-roster-dialog-plant-id").value = result.plant ? result.plant.id : "";
        if (slots.length) dateInput.value = slots[0].date;
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
      if (zone.dataset.date && dateMatchesCategoryForZone(zone.dataset.date, zone.dataset.dayType)) return zone.dataset.date;
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

    function dateMatchesCategoryForZone(dateValue, dayType) {
      var day = new Date(dateValue + "T12:00:00").getDay();
      if (dayType === "weekday") return day !== 0 && day !== 6;
      if (dayType === "weekend") return day === 0 || day === 6;
      return true;
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
      dragged = { id: chip.dataset.assignmentId, staffId: chip.dataset.staffId, date: chip.dataset.date, shiftId: chip.dataset.shiftId, plantId: chip.dataset.plantId, sectionId: chip.dataset.sectionId, weekStart: chip.dataset.weekStart, weekEnd: chip.dataset.weekEnd, supervisorEligible: chip.dataset.supervisorEligible === "true" };
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
      var zone = event.target.closest("[data-roster-dropzone], [data-supervisor-dropzone], [data-leave-dropzone]");
      if (!zone || !dragged || (zone.matches("[data-roster-dropzone]") && zone.dataset.hasDates === "false")) return;
      if (zone.matches("[data-supervisor-dropzone]") && !canManageSupervisors) return;
      if (zone.matches("[data-leave-dropzone]") && !canEditRoster && !canManageSupervisors) return;
      event.preventDefault();
      zone.classList.add("is-drop-target");
    });
    root.addEventListener("dragleave", function (event) {
      var zone = event.target.closest("[data-roster-dropzone], [data-supervisor-dropzone], [data-leave-dropzone]");
      if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove("is-drop-target");
    });
    root.addEventListener("drop", async function (event) {
      var zone = event.target.closest("[data-roster-dropzone], [data-supervisor-dropzone], [data-leave-dropzone]");
      if (!zone || !dragged) return;

      if (zone.matches("[data-leave-dropzone]")) {
        event.preventDefault();
        zone.classList.remove("is-drop-target");
        if (!canEditRoster && !canManageSupervisors) return notify("You do not have permission to manage staff availability.", true);
        if (dragged.plantId && zone.dataset.plantId && dragged.plantId !== zone.dataset.plantId) {
          return notify("Staff availability must remain within the same treatment plant.", true);
        }
        try {
          await postAction({
            action: "leave",
            staff_id: dragged.staffId,
            assignment_id: dragged.id,
            plant_id: zone.dataset.plantId || dragged.plantId,
            week_start: zone.dataset.weekStart,
            week_end: zone.dataset.weekEnd
          });
          notify("Staff member placed on annual leave. Refreshing the roster…", false);
          window.setTimeout(function () { window.location.reload(); }, 500);
        } catch (error) { notify(error.message, true); }
        return;
      }

      if (zone.matches("[data-supervisor-dropzone]")) {
        event.preventDefault();
        zone.classList.remove("is-drop-target");
        if (!canManageSupervisors) return notify("You do not have permission to assign a weekly shift supervisor.", true);
        if (dragged.plantId !== zone.dataset.plantId) return notify("Supervisors must remain within the same treatment plant.", true);
        if (dragged.sectionId !== zone.dataset.sectionId) return notify("Supervisors must remain within the same section.", true);
        if (dragged.weekStart !== zone.dataset.weekStart || dragged.weekEnd !== zone.dataset.weekEnd) return notify("Choose a user assigned during this roster week.", true);
        if (!dragged.supervisorEligible) return notify("This staff member is not registered as a section supervisor. Add them as a Shift Supervisor in admin first.", true);
        try {
          await postAction({
            action: "supervisor",
            user_id: dragged.staffId,
            assignment_id: dragged.id,
            shift_id: zone.dataset.shiftId,
            section_id: zone.dataset.sectionId,
            plant_id: zone.dataset.plantId,
            week_start: zone.dataset.weekStart,
            week_end: zone.dataset.weekEnd
          });
          notify("Weekly shift supervisor assigned. Refreshing the roster…", false);
          window.setTimeout(function () { window.location.reload(); }, 500);
        } catch (error) { notify(error.message, true); }
        return;
      }
      event.preventDefault();
      zone.classList.remove("is-drop-target");
      if (!canEditRoster) return notify("You do not have permission to move duty assignments.", true);
      if (zone.dataset.hasDates === "false") return notify(zone.dataset.noDatesLabel || "No matching dates exist in that roster cell.", true);
      if (dragged.plantId && dragged.plantId !== zone.dataset.plantId) return notify("Duty assignments cannot be moved between treatment plants.", true);
      if (dragged.sectionId && dragged.sectionId !== zone.dataset.sectionId) return notify("Duty assignments cannot be moved between sections.", true);
      if (dragged.date === zone.dataset.date && dragged.shiftId === zone.dataset.shiftId) return notify("Choose a different duty date or shift for this assignment.", true);
      var date = candidateDate(zone, dragged.date);
      if (!date) return notify("No matching dates exist in that roster cell.", true);
      try {
        await postAction({ action: "move", assignment_id: dragged.id, shift_id: zone.dataset.shiftId, plant_id: zone.dataset.plantId, date: date, day_type: zone.dataset.dayType, week_start: zone.dataset.weekStart, week_end: zone.dataset.weekEnd });
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
            plant_id: document.getElementById("sc-roster-dialog-plant-id").value,
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

    var balanceBtn = document.getElementById("scBalanceSupervisorsBtn");
    if (balanceBtn) {
      balanceBtn.addEventListener("click", async function (event) {
        event.preventDefault();
        var plantId = balanceBtn.dataset.plantId;
        var sectionId = balanceBtn.dataset.sectionId;
        var year = balanceBtn.dataset.year;
        var month = balanceBtn.dataset.month;
        if (!plantId || !sectionId) {
          return notify("Please select a specific treatment plant and section.", true);
        }
        var confirmed = window.confirm(
          "Dynamically balance supervisory workload across all qualified shift supervisors for this section and month?"
        );
        if (!confirmed) return;

        var origHtml = balanceBtn.innerHTML;
        balanceBtn.disabled = true;
        balanceBtn.innerHTML = '<i class="fas fa-spinner fa-spin" aria-hidden="true"></i> Balancing…';
        try {
          var response = await postAction({
            action: "balance_supervisors",
            plant_id: plantId,
            section_id: sectionId,
            year: year,
            month: month,
            overwrite_existing: false,
          });
          notify(response.message || "Supervisors balanced successfully. Refreshing roster…", false);
          window.setTimeout(function () {
            window.location.reload();
          }, 800);
        } catch (error) {
          notify(error.message, true);
          balanceBtn.disabled = false;
          balanceBtn.innerHTML = origHtml;
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    cascadeFilters();
    rosterActions();
  });
})();
