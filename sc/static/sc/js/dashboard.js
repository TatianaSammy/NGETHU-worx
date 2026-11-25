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
    select.replaceChildren();
    if (!select.multiple) select.add(new Option(label, ""));
    var options = document.createDocumentFragment();
    items.forEach(function (item) {
      options.appendChild(new Option(item.label || item.name, item.value || item.id));
    });
    select.appendChild(options);
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
    var swapDialog = document.getElementById("sc-roster-swap-dialog");
    var swapForm = document.getElementById("sc-roster-swap-form");
    var pendingSwapResolve = null;
    var tempSupervisorDialog = document.getElementById("sc-temporary-supervisor-dialog");
    var tempSupervisorForm = document.getElementById("sc-temporary-supervisor-form");
    var availabilityDialog = document.getElementById("sc-availability-dialog");
    var availabilityForm = document.getElementById("sc-availability-form");
    var toast = document.getElementById("sc-roster-toast");
    var dragged = null;
    var selectedCrewScope = "";
    var slots = [];
    var availabilityMessage = "";
    var lockedSectionId = "";
    var preferredSectionId = "";
    var maxSelectableStaff = 0;
    var canSelectSection = root.dataset.superuser === "true";
    var canManageSupervisors = root.dataset.canManageSupervisors === "true";
    var canManageTempSupervisors = root.dataset.canManageTempSupervisors === "true";
    var canManageAvailability = root.dataset.canManageAvailability === "true";
    var canEditRoster = root.dataset.canEditRoster === "true";
    var messageTimer;
    var addOptionsRequestId = 0;

    function notify(message, error) {
      if (!toast) return;
      toast.textContent = message;
      toast.dataset.state = error ? "error" : "success";
      toast.classList.add("is-visible");
      window.clearTimeout(messageTimer);
      messageTimer = window.setTimeout(function () { toast.classList.remove("is-visible"); }, 4200);
    }

    function crewScopeKey(chip) {
      return [chip.dataset.plantId, chip.dataset.sectionId, chip.dataset.weekStart, chip.dataset.weekEnd].join(":");
    }

    function clearCrewSelection() {
      root.querySelectorAll("[data-assignment-id].is-roster-selected").forEach(function (chip) {
        chip.classList.remove("is-roster-selected");
      });
      selectedCrewScope = "";
    }

    function selectCrewMember(chip, selected) {
      var staffId = chip.dataset.staffId;
      var scope = crewScopeKey(chip);
      root.querySelectorAll("[data-assignment-id]").forEach(function (candidate) {
        if (candidate.dataset.staffId === staffId && crewScopeKey(candidate) === scope) {
          candidate.classList.toggle("is-roster-selected", selected);
        }
      });
    }

    function selectedCrewStaffIds(chip) {
      var scope = crewScopeKey(chip);
      var ids = new Set();
      root.querySelectorAll("[data-assignment-id].is-roster-selected").forEach(function (candidate) {
        if (crewScopeKey(candidate) === scope) ids.add(candidate.dataset.staffId);
      });
      ids.add(chip.dataset.staffId);
      return Array.from(ids);
    }

    function selectedCrewSourceShiftIds(chip, staffIds) {
      var scope = crewScopeKey(chip);
      var shiftIds = new Set();
      root.querySelectorAll("[data-assignment-id]").forEach(function (candidate) {
        if (staffIds.indexOf(candidate.dataset.staffId) >= 0 && crewScopeKey(candidate) === scope) {
          shiftIds.add(candidate.dataset.shiftId);
        }
      });
      return Array.from(shiftIds);
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
      return !dateValue || slots.some(function (slot) { return slot.date === dateValue; });
    }

    function updateUsers() {
      var slot = selectedDateSlot();
      var dateInput = document.getElementById("sc-roster-dialog-date");
      var sectionId = document.getElementById("sc-roster-dialog-section").value;
      var section = slot && slot.sections.find(function (item) { return String(item.id) === sectionId; });
      var staffSelect = document.getElementById("sc-roster-dialog-staff");
      var staff = section ? section.users.map(function (person) { return { id: person.id, name: person.name }; }) : [];
      setOptions(staffSelect, staff, staff.length ? "Choose a staff member" : "No available staff");
      var sectionCapacityByDate = slots.map(function (weekSlot) {
        var weekSection = weekSlot.sections.find(function (item) { return String(item.id) === sectionId; });
        return weekSection ? Number(weekSection.max_selectable) : 0;
      });
      maxSelectableStaff = sectionCapacityByDate.length
        ? Math.max(0, Math.min(staff.length, Math.min.apply(Math, sectionCapacityByDate)))
        : 0;
      renderStaffPicker();
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
            ? maxSelectableStaff + (maxSelectableStaff === 1 ? " slot open" : " slots open")
            : section.capacity_message || availabilityMessage || "No eligible staff are available for this category.";
      document.getElementById("sc-roster-dialog-status").textContent = statusMessage;
      form.querySelector('[type="submit"]').disabled = staff.length === 0 || maxSelectableStaff < 1 || !staffSelect.selectedOptions.length || !dateInput.value || !dateMatches;
    }

    function syncStaffPickerActions() {
      var picker = document.getElementById("sc-roster-dialog-staff-options");
      if (!picker) return;
      var staffSelect = document.getElementById("sc-roster-dialog-staff");
      var selectedCount = staffSelect.selectedOptions.length;
      var fillButton = document.getElementById("sc-roster-staff-fill-slots");
      var clearButton = document.getElementById("sc-roster-staff-clear");
      if (fillButton) fillButton.disabled = staffSelect.disabled || selectedCount >= maxSelectableStaff;
      if (clearButton) clearButton.disabled = selectedCount === 0;
    }

    function renderStaffPicker() {
      var staffSelect = document.getElementById("sc-roster-dialog-staff");
      var picker = document.getElementById("sc-roster-dialog-staff-options");
      if (!staffSelect || !picker) return;
      picker.replaceChildren();
      var options = document.createDocumentFragment();
      Array.from(staffSelect.options).forEach(function (option) {
        if (!option.value) return;
        var label = document.createElement("label");
        label.className = "sc-roster-staff-option";
        var checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = option.value;
        checkbox.checked = option.selected;
        checkbox.disabled = staffSelect.disabled || maxSelectableStaff < 1;
        checkbox.addEventListener("change", function () {
          var sourceOption = Array.from(staffSelect.options).find(function (item) {
            return item.value === checkbox.value;
          });
          if (!sourceOption) return;
          if (checkbox.checked && staffSelect.selectedOptions.length >= maxSelectableStaff) {
            checkbox.checked = false;
            document.getElementById("sc-roster-dialog-status").textContent =
              "This category has room for at most " + maxSelectableStaff + " additional staff across its dates.";
            return;
          }
          sourceOption.selected = checkbox.checked;
          enforceStaffSelectionLimit();
        });
        var name = document.createElement("span");
        name.textContent = option.textContent;
        label.append(checkbox, name);
        options.appendChild(label);
      });
      if (!staffSelect.options.length) {
        var empty = document.createElement("div");
        empty.className = "sc-roster-staff-empty";
        empty.textContent = availabilityMessage || "No eligible staff are available for this category.";
        options.appendChild(empty);
      }
      picker.appendChild(options);
      syncStaffPickerActions();
    }

    function enforceStaffSelectionLimit() {
      var staffSelect = document.getElementById("sc-roster-dialog-staff");
      var selected = Array.from(staffSelect.selectedOptions);
      var status = document.getElementById("sc-roster-dialog-status");
      var exceededLimit = selected.length > maxSelectableStaff;
      if (exceededLimit) {
        selected.slice(maxSelectableStaff).forEach(function (option) { option.selected = false; });
        selected = Array.from(staffSelect.selectedOptions);
        status.textContent = "This category has room for at most " + maxSelectableStaff + " additional staff across its dates.";
      } else {
        status.textContent = selected.length
          ? selected.length + " selected · " + maxSelectableStaff + " max"
          : maxSelectableStaff > 0
            ? maxSelectableStaff + (maxSelectableStaff === 1 ? " slot open" : " slots open")
            : staffSelect.options.length
              ? "No slots open"
            : availabilityMessage || "No eligible staff are available for this category.";
      }
      var dateInput = document.getElementById("sc-roster-dialog-date");
      form.querySelector('[type="submit"]').disabled = !selected.length || selected.length > maxSelectableStaff || !dateInput.value || !dateMatchesCategory(dateInput.value);
      Array.from(document.querySelectorAll("#sc-roster-dialog-staff-options input[type='checkbox']")).forEach(function (checkbox) {
        checkbox.checked = Array.from(staffSelect.selectedOptions).some(function (option) {
          return option.value === checkbox.value;
        });
      });
      syncStaffPickerActions();
    }

    async function openAdd(zone) {
      if (!dialog) return;
      var requestId = ++addOptionsRequestId;
      var status = document.getElementById("sc-roster-dialog-status");
      slots = [];
      maxSelectableStaff = 0;
      availabilityMessage = "Loading eligible staff…";
      setOptions(document.getElementById("sc-roster-dialog-staff"), [], "No available staff");
      setOptions(document.getElementById("sc-roster-dialog-section"), [], "Loading sections…");
      renderStaffPicker();
      form.querySelector('[type="submit"]').disabled = true;
      status.textContent = "Loading eligible staff…";
      document.getElementById("sc-roster-dialog-subtitle").textContent = zone.dataset.category + " · " + zone.dataset.weekStart + " to " + zone.dataset.weekEnd;
      document.getElementById("sc-roster-dialog-plant").textContent = "Loading…";
      document.getElementById("sc-roster-dialog-shift").value = zone.dataset.shiftId;
      document.getElementById("sc-roster-dialog-day-type").value = zone.dataset.dayType;
      document.getElementById("sc-roster-dialog-week-start").value = zone.dataset.weekStart;
      document.getElementById("sc-roster-dialog-week-end").value = zone.dataset.weekEnd;
      var dateInput = document.getElementById("sc-roster-dialog-date");
      document.getElementById("sc-roster-dialog-staff-label").textContent = "Available staff";
      document.getElementById("sc-roster-dialog-submit-label").textContent = "Add selected";
      dateInput.min = zone.dataset.date || zone.dataset.weekStart;
      dateInput.max = zone.dataset.date || zone.dataset.weekEnd;
      dateInput.value = zone.dataset.date || "";
      lockedSectionId = canSelectSection ? "" : zone.dataset.sectionId || "";
      preferredSectionId = zone.dataset.sectionId || "";
      if (!dialog.open) dialog.showModal();
      try {
        var url = new URL(root.dataset.optionsUrl, window.location.origin);
        url.searchParams.set("shift_id", zone.dataset.shiftId);
        url.searchParams.set("plant_id", zone.dataset.plantId);
        url.searchParams.set("week_start", zone.dataset.weekStart);
        url.searchParams.set("week_end", zone.dataset.weekEnd);
        url.searchParams.set("day_type", zone.dataset.dayType);
        if (zone.dataset.date) url.searchParams.set("date", zone.dataset.date);
        if (canSelectSection) {
          if (preferredSectionId) url.searchParams.set("preferred_section_id", preferredSectionId);
        } else if (lockedSectionId) {
          url.searchParams.set("section_id", lockedSectionId);
        }
        var response = await fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } });
        var result;
        try {
          result = await response.json();
        } catch (_error) {
          throw new Error("The staff list could not be loaded.");
        }
        if (requestId !== addOptionsRequestId) return;
        if (!response.ok || !result.ok) throw new Error(result.message || "Unable to load roster candidates.");
        slots = result.slots || [];
        availabilityMessage = result.message || "";
        lockedSectionId = result.locked_section_id ? String(result.locked_section_id) : "";
        preferredSectionId = result.preferred_section_id ? String(result.preferred_section_id) : preferredSectionId;
        canSelectSection = Boolean(result.can_select_section);
        document.getElementById("sc-roster-dialog-plant").textContent = result.plant ? result.plant.name : "Plant unavailable";
        document.getElementById("sc-roster-dialog-plant-id").value = result.plant ? result.plant.id : "";
        if (slots.length) {
          var anchorSlot = slots.find(function (slot) { return slot.day_type === zone.dataset.dayType; });
          anchorSlot = anchorSlot || slots[0];
          dateInput.value = anchorSlot.date;
        }
        updateSections();
      } catch (error) {
        if (requestId !== addOptionsRequestId) return;
        availabilityMessage = error.message;
        form.querySelector('[type="submit"]').disabled = true;
        status.textContent = error.message;
        var empty = document.querySelector("#sc-roster-dialog-staff-options .sc-roster-staff-empty");
        if (empty) empty.textContent = error.message;
      }
    }

    function candidateDate(zone, sourceDate) {
      var dates = (zone.dataset.categoryDates || "").split(",").filter(Boolean);
      if (!dates.length) return "";
      var source = new Date(sourceDate + "T12:00:00");
      var preferred = dates.find(function (item) {
        return new Date(item + "T12:00:00").getDay() === source.getDay();
      });
      return preferred || dates[0];
    }

    async function postAction(payload) {
      var response = await fetch(root.dataset.actionUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken"), "X-Requested-With": "XMLHttpRequest" },
        body: JSON.stringify(payload)
      });
      var result;
      try {
        result = await response.json();
      } catch (_error) {
        throw new Error("The server returned an unreadable response. Refresh the roster before retrying to confirm whether the update completed.");
      }
      if (!response.ok || !result.ok) {
        var error = new Error(result.message || "The roster could not be updated.");
        error.payload = result;
        throw error;
      }
      return result;
    }

    function applyAddedAssignments(result) {
      var assignments = Array.isArray(result.assignments) ? result.assignments : [];
      var applied = 0;
      assignments.forEach(function (item) {
        var zone = Array.from(root.querySelectorAll("[data-roster-dropzone]")).find(function (candidate) {
          return candidate.dataset.shiftId === String(item.shift_id)
            && candidate.dataset.sectionId === String(item.section_id)
            && candidate.dataset.plantId === String(item.plant_id)
            && candidate.dataset.weekStart === item.week_start
            && candidate.dataset.weekEnd === item.week_end
            && candidate.dataset.dayType === item.day_type;
        });
        if (!zone) return;
        var list = zone.querySelector(".sc-roster-assignment-list");
        if (!list) {
          list = document.createElement("div");
          list.className = "sc-roster-assignment-list sc-roster-inline-names";
          zone.prepend(list);
        }
        var alreadyRendered = Array.from(list.querySelectorAll("[data-assignment-id]")).some(function (chip) {
          return chip.dataset.assignmentId === String(item.id)
            || chip.dataset.staffId === String(item.staff_id);
        });
        if (!alreadyRendered) {
          var chip = document.createElement("span");
          chip.className = "sc-assignment-staff sc-staff-inline-item";
          chip.setAttribute("draggable", canEditRoster || canManageSupervisors || canManageAvailability ? "true" : "false");
          chip.dataset.assignmentId = String(item.id);
          chip.dataset.staffId = String(item.staff_id);
          chip.dataset.date = item.date;
          chip.dataset.shiftId = String(item.shift_id);
          chip.dataset.sectionId = String(item.section_id);
          chip.dataset.plantId = String(item.plant_id);
          chip.dataset.weekStart = item.week_start;
          chip.dataset.weekEnd = item.week_end;
          chip.dataset.attendance = "present";
          chip.dataset.supervisorEligible = item.supervisor_eligible ? "true" : "false";
          chip.title = item.staff_name + " · " + item.date;
          var name = document.createElement("span");
          name.className = "sc-staff-name";
          name.textContent = item.staff_name;
          chip.appendChild(name);
          list.appendChild(chip);
          applied += 1;
        }

        if (item.supervisor_eligible) {
          root.querySelectorAll("[data-supervisor-dropzone]").forEach(function (supervisorZone) {
            if (supervisorZone.dataset.shiftId !== String(item.shift_id)
              || supervisorZone.dataset.sectionId !== String(item.section_id)
              || supervisorZone.dataset.plantId !== String(item.plant_id)
              || supervisorZone.dataset.weekStart !== item.week_start
              || supervisorZone.dataset.weekEnd !== item.week_end) return;
            var select = supervisorZone.querySelector("[data-supervisor-user]");
            if (!select || Array.from(select.options).some(function (option) {
              return option.value === String(item.staff_id);
            })) return;
            var option = new Option(item.staff_name, String(item.staff_id));
            option.dataset.assignmentId = String(item.id);
            select.add(option);
          });
        }
      });
      return applied;
    }

    function selectedSwapStaff() {
      var select = document.getElementById("sc-roster-swap-staff");
      return Array.from(select.selectedOptions).map(function (option) { return option.value; });
    }

    function updateSwapSubmit() {
      if (!swapForm) return;
      var expected = Number(swapForm.dataset.swapCount || 0);
      swapForm.querySelector('[type="submit"]').disabled = selectedSwapStaff().length !== expected;
    }

    function promptForSwap(payload) {
      if (!swapDialog || !swapForm) return Promise.resolve(null);
      var select = document.getElementById("sc-roster-swap-staff");
      var status = document.getElementById("sc-roster-swap-status");
      var expected = Number(payload.swap_count || 1);
      var candidates = payload.swap_candidates || [];
      swapForm.dataset.swapCount = String(expected);
      select.multiple = expected > 1;
      select.size = Math.min(8, Math.max(3, candidates.length));
      select.replaceChildren();
      candidates.forEach(function (person) {
        select.add(new Option(person.schedule ? person.name + " · " + person.schedule : person.name, String(person.id)));
      });
      select.selectedIndex = -1;
      status.textContent = "";
      document.getElementById("sc-roster-swap-instructions").textContent =
        "Select " + expected + (expected === 1 ? " staff member" : " staff members") + " to exchange with the dragged crew.";
      swapForm.querySelector('[type="submit"]').disabled = true;
      if (!candidates.length) {
        status.textContent = "No eligible staff are available to swap.";
        return Promise.resolve(null);
      }
      return new Promise(function (resolve) {
        pendingSwapResolve = resolve;
        swapDialog.showModal();
      });
    }

    function finishSwap(staffIds) {
      if (!swapDialog || !swapDialog.open) return;
      swapDialog.close();
      if (pendingSwapResolve) {
        var resolve = pendingSwapResolve;
        pendingSwapResolve = null;
        resolve(staffIds);
      }
    }

    if (swapForm) {
      document.getElementById("sc-roster-swap-staff").addEventListener("change", updateSwapSubmit);
      swapForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var staffIds = selectedSwapStaff();
        var expected = Number(swapForm.dataset.swapCount || 0);
        if (staffIds.length !== expected) {
          document.getElementById("sc-roster-swap-status").textContent =
            "Select exactly " + expected + (expected === 1 ? " staff member." : " staff members.");
          return;
        }
        finishSwap(staffIds);
      });
      swapForm.addEventListener("click", function (event) {
        if (event.target.closest("[data-close-roster-swap]")) finishSwap(null);
      });
      swapDialog.addEventListener("cancel", function () {
        if (pendingSwapResolve) {
          var resolve = pendingSwapResolve;
          pendingSwapResolve = null;
          resolve(null);
        }
      });
    }

    root.addEventListener("click", function (event) {
      if (event.target.closest("[data-close-availability]") && availabilityDialog && availabilityDialog.open) {
        availabilityDialog.close();
      }
    });

    if (availabilityForm) {
      availabilityForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        var submit = availabilityForm.querySelector('[type="submit"]');
        var statusMessage = document.getElementById("sc-availability-message");
        submit.disabled = true;
        try {
          var result = await postAction({
            action: "leave",
            staff_id: document.getElementById("sc-availability-staff-id").value,
            plant_id: document.getElementById("sc-availability-plant-id").value,
            week_start: document.getElementById("sc-availability-week-start").value,
            week_end: document.getElementById("sc-availability-week-end").value,
            status: document.getElementById("sc-availability-status").value
          });
          if (availabilityDialog.open) availabilityDialog.close();
          notify(result.message || "Staff availability updated. Refreshing the roster…", false);
          window.setTimeout(function () { window.location.reload(); }, 500);
        } catch (error) {
          statusMessage.textContent = error.message;
          statusMessage.dataset.state = "error";
          submit.disabled = false;
        }
      });
    }

    root.addEventListener("click", async function (event) {
      var trigger = event.target.closest("[data-open-temporary-supervisor]");
      var closeButton = event.target.closest("[data-close-temporary-supervisor]");
      if (closeButton) {
        if (tempSupervisorDialog && tempSupervisorDialog.open) tempSupervisorDialog.close();
        return;
      }
      if (!trigger) return;
      event.preventDefault();
      if (!canManageTempSupervisors || !tempSupervisorDialog) return notify("You do not have permission to appoint a temporary supervisor.", true);
      var supervisorSelect = document.getElementById("sc-temporary-supervisor-user");
      var status = document.getElementById("sc-temporary-supervisor-status");
      var submit = tempSupervisorForm.querySelector('[type="submit"]');
      var assignmentId = document.getElementById("sc-temporary-supervisor-assignment-id");
      assignmentId.value = trigger.dataset.tempAssignmentId;
      tempSupervisorDialog.dataset.plantId = trigger.dataset.tempPlantId;
      tempSupervisorDialog.dataset.sectionId = trigger.dataset.tempSectionId;
      tempSupervisorDialog.dataset.shiftId = trigger.dataset.tempShiftId;
      tempSupervisorDialog.dataset.date = trigger.dataset.tempDate;
      document.getElementById("sc-temporary-supervisor-subtitle").textContent =
        "Today's duty · " + trigger.dataset.tempDate + " · temporary coverage for up to three hours.";
      status.textContent = "Loading qualified supervisors…";
      submit.disabled = true;
      try {
        var url = new URL(root.dataset.supervisorOptionsUrl, window.location.origin);
        url.searchParams.set("plant_id", trigger.dataset.tempPlantId);
        url.searchParams.set("section_id", trigger.dataset.tempSectionId);
        url.searchParams.set("shift_id", trigger.dataset.tempShiftId);
        url.searchParams.set("date", trigger.dataset.tempDate);
        var response = await fetch(url.toString(), { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } });
        var result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.message || "Could not load eligible supervisors.");
        supervisorSelect.replaceChildren(new Option(result.users.length ? "Choose a supervisor" : "No eligible supervisors", ""));
        result.users.forEach(function (user) { supervisorSelect.add(new Option(user.label, user.id)); });
        supervisorSelect.disabled = result.users.length === 0;
        submit.disabled = result.users.length === 0;
        status.textContent = result.users.length
          ? result.users.length + " eligible supervisor(s) available. They must have no routine roster or task assignment today."
          : "No eligible supervisors are free from routine work today.";
        tempSupervisorDialog.showModal();
      } catch (error) {
        status.textContent = error.message;
        status.dataset.state = "error";
        tempSupervisorDialog.showModal();
      }
    });

    if (tempSupervisorForm) {
      tempSupervisorForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        var supervisorSelect = document.getElementById("sc-temporary-supervisor-user");
        var supervisorId = supervisorSelect.value;
        if (!supervisorId) return;
        var submit = tempSupervisorForm.querySelector('[type="submit"]');
        var status = document.getElementById("sc-temporary-supervisor-status");
        submit.disabled = true;
        try {
          var result = await postAction({
            action: "temporary_supervisor",
            assignment_id: document.getElementById("sc-temporary-supervisor-assignment-id").value,
            user_id: supervisorId,
            duration_hours: document.getElementById("sc-temporary-supervisor-duration").value
          });
          if (tempSupervisorDialog.open) tempSupervisorDialog.close();
          notify(result.message || "Temporary supervisor appointed. Refreshing the roster…", false);
          window.setTimeout(function () { window.location.reload(); }, 500);
        } catch (error) {
          status.textContent = error.message;
          status.dataset.state = "error";
          submit.disabled = false;
        }
      });
    }

    root.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-open-roster-add]");
      if (trigger) openAdd(trigger.closest("[data-roster-dropzone]"));
    });
    root.addEventListener("click", async function (event) {
      var openPicker = event.target.closest("[data-open-supervisor-picker]");
      if (openPicker) {
        var supervisorCell = openPicker.closest("[data-supervisor-dropzone]");
        var picker = supervisorCell && supervisorCell.querySelector("[data-supervisor-picker]");
        if (picker) {
          picker.hidden = false;
          openPicker.hidden = true;
          picker.querySelector("select")?.focus();
        }
        return;
      }
      var cancelPicker = event.target.closest("[data-cancel-supervisor-picker]");
      if (cancelPicker) {
        var cancelCell = cancelPicker.closest("[data-supervisor-dropzone]");
        var cancelForm = cancelCell && cancelCell.querySelector("[data-supervisor-picker]");
        if (cancelForm) cancelForm.hidden = true;
        var addButton = cancelCell && cancelCell.querySelector("[data-open-supervisor-picker]");
        if (addButton) addButton.hidden = false;
        return;
      }
      var saveSupervisor = event.target.closest("[data-save-supervisor]");
      if (!saveSupervisor) return;
      var cell = saveSupervisor.closest("[data-supervisor-dropzone]");
      var supervisorSelect = cell && cell.querySelector("[data-supervisor-user]");
      var supervisorOption = supervisorSelect && supervisorSelect.selectedOptions[0];
      if (!supervisorOption || !supervisorOption.value) return notify("Choose a rostered supervisor for this shift week.", true);
      saveSupervisor.disabled = true;
      try {
        var result = await postAction({
          action: "supervisor",
          user_id: supervisorOption.value,
          assignment_id: supervisorOption.dataset.assignmentId,
          shift_id: cell.dataset.shiftId,
          section_id: cell.dataset.sectionId,
          plant_id: cell.dataset.plantId,
          week_start: cell.dataset.weekStart,
          week_end: cell.dataset.weekEnd
        });
        notify(result.message || "Weekly shift supervisor assigned. Refreshing the roster…", false);
        window.setTimeout(function () { window.location.reload(); }, 500);
      } catch (error) {
        notify(error.message, true);
        saveSupervisor.disabled = false;
      }
    });
    root.addEventListener("click", async function (event) {
      var trigger = event.target.closest("[data-autopopulate-year]");
      if (!trigger) return;
      event.preventDefault();
      if (!canEditRoster) return notify("You do not have permission to generate the duty roster.", true);
      if (trigger.disabled) return;
      trigger.disabled = true;
      var originalLabel = trigger.innerHTML;
      trigger.innerHTML = '<i class="fas fa-spinner fa-spin" aria-hidden="true"></i> Generating full year…';
      notify("Generating and populating all 12 monthly rosters. This may take a little while.", false);
      try {
        var result = await postAction({
          action: "autopopulate_year",
          plant_id: trigger.dataset.plantId,
          section_id: trigger.dataset.sectionId,
          year: trigger.dataset.year
        });
        notify(result.message || "The full year duty roster has been generated. Refreshing the roster…", false);
        window.setTimeout(function () { window.location.reload(); }, 650);
      } catch (error) {
        trigger.innerHTML = originalLabel;
        trigger.disabled = false;
        notify(error.message, true);
      }
    });
    root.addEventListener("contextmenu", function (event) {
      var zone = event.target.closest("[data-roster-dropzone]");
      if (!zone || !dialog) return;
      event.preventDefault();
      if (zone.dataset.hasDates === "false") return notify(zone.dataset.noDatesLabel || "No matching dates exist in this roster cell.", true);
      openAdd(zone);
    });

    root.addEventListener("click", function (event) {
      var chip = event.target.closest("[data-assignment-id]");
      if (!chip) return;
      var modified = event.ctrlKey || event.metaKey || event.shiftKey;
      if (!modified) {
        if (!chip.classList.contains("is-roster-selected")) clearCrewSelection();
        return;
      }
      event.preventDefault();
      var scope = crewScopeKey(chip);
      if (selectedCrewScope !== scope) clearCrewSelection();
      selectedCrewScope = scope;
      var alreadySelected = Array.from(root.querySelectorAll("[data-assignment-id].is-roster-selected"))
        .some(function (candidate) { return crewScopeKey(candidate) === scope && candidate.dataset.staffId === chip.dataset.staffId; });
      selectCrewMember(chip, !alreadySelected);
    });

    root.addEventListener("dragstart", function (event) {
      var chip = event.target.closest("[data-assignment-id]");
      if (!chip || chip.getAttribute("draggable") !== "true") return;
      var sourceZone = chip.closest("[data-roster-dropzone]");
      var staffIds = selectedCrewStaffIds(chip);
      dragged = {
        id: chip.dataset.assignmentId,
        staffId: chip.dataset.staffId,
        staffName: chip.querySelector(".sc-staff-name")?.textContent.trim() || "Staff member",
        staffIds: staffIds,
        sourceShiftIds: selectedCrewSourceShiftIds(chip, staffIds),
        date: chip.dataset.date,
        shiftId: chip.dataset.shiftId || (sourceZone ? sourceZone.dataset.shiftId : ""),
        plantId: chip.dataset.plantId || (sourceZone ? sourceZone.dataset.plantId : ""),
        sectionId: chip.dataset.sectionId || (sourceZone ? sourceZone.dataset.sectionId : ""),
        weekStart: chip.dataset.weekStart || (sourceZone ? sourceZone.dataset.weekStart : ""),
        weekEnd: chip.dataset.weekEnd || (sourceZone ? sourceZone.dataset.weekEnd : ""),
        dayType: sourceZone ? sourceZone.dataset.dayType : "",
        categoryDates: sourceZone ? sourceZone.dataset.categoryDates : "",
        supervisorEligible: chip.dataset.supervisorEligible === "true"
      };
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", JSON.stringify({ staff_ids: staffIds }));
      root.querySelectorAll("[data-assignment-id]").forEach(function (candidate) {
        if (staffIds.indexOf(candidate.dataset.staffId) >= 0 && crewScopeKey(candidate) === crewScopeKey(chip)) {
          candidate.classList.add("is-dragging");
        }
      });
    });
    root.addEventListener("dragend", function (event) {
      root.querySelectorAll("[data-assignment-id].is-dragging").forEach(function (chip) { chip.classList.remove("is-dragging"); });
      root.querySelectorAll(".is-drop-target").forEach(function (item) { item.classList.remove("is-drop-target"); });
      dragged = null;
      clearCrewSelection();
    });
    root.addEventListener("dragover", function (event) {
      var zone = event.target.closest("[data-roster-dropzone], [data-supervisor-dropzone], [data-leave-dropzone]");
      if (!zone || !dragged || (zone.matches("[data-roster-dropzone]") && zone.dataset.hasDates === "false")) return;
      if (zone.matches("[data-supervisor-dropzone]") && !canManageSupervisors) return;
      if (zone.matches("[data-leave-dropzone]") && !canManageAvailability) return;
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
        if (dragged.staffIds.length > 1) return notify("Move a selected group onto another shift. Availability status applies to one staff member at a time.", true);
        if (!canManageAvailability || !availabilityDialog) return notify("You do not have permission to manage staff availability.", true);
        if (dragged.plantId && zone.dataset.plantId && dragged.plantId !== zone.dataset.plantId) {
          return notify("Staff availability must remain within the same treatment plant.", true);
        }
        document.getElementById("sc-availability-staff-id").value = dragged.staffId;
        document.getElementById("sc-availability-plant-id").value = zone.dataset.plantId || dragged.plantId;
        document.getElementById("sc-availability-week-start").value = zone.dataset.weekStart;
        document.getElementById("sc-availability-week-end").value = zone.dataset.weekEnd;
        document.getElementById("sc-availability-subtitle").textContent =
          dragged.staffName + " · " + zone.dataset.weekStart + " to " + zone.dataset.weekEnd;
        document.getElementById("sc-availability-status").value = "annual_leave";
        document.getElementById("sc-availability-message").textContent =
          "In-charge staff are exempt from routine shifts and weekly tasks for this period.";
        document.getElementById("sc-availability-message").dataset.state = "";
        availabilityDialog.showModal();
        return;
      }

      if (zone.matches("[data-supervisor-dropzone]")) {
        event.preventDefault();
        zone.classList.remove("is-drop-target");
        if (dragged.staffIds.length > 1) return notify("Assign one supervisor at a time. Drag a selected group to a duty shift cell.", true);
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
      if (dragged.plantId && zone.dataset.plantId && dragged.plantId !== zone.dataset.plantId) return notify("Duty assignments cannot be moved between treatment plants.", true);

      // Same cell drop check
      if (
        dragged.shiftId === zone.dataset.shiftId &&
        dragged.weekStart === zone.dataset.weekStart &&
        dragged.weekEnd === zone.dataset.weekEnd &&
        dragged.dayType === zone.dataset.dayType &&
        dragged.staffIds.length === 1
      ) {
        return notify("Staff member is already assigned to this shift and category.", false);
      }

      var date = candidateDate(zone, dragged.date);
      if (!date && zone.dataset.categoryDates) {
        date = zone.dataset.categoryDates.split(",").filter(Boolean)[0];
      }
      if (!date) return notify("No matching dates exist in that roster cell.", true);

      var movedStaffCount = dragged.staffIds.length;
      var movePayload = {
        action: "move_team",
        staff_ids: dragged.staffIds,
        section_id: zone.dataset.sectionId || dragged.sectionId,
        shift_id: zone.dataset.shiftId,
        plant_id: zone.dataset.plantId || dragged.plantId,
        date: date,
        day_type: zone.dataset.dayType,
        week_start: zone.dataset.weekStart,
        week_end: zone.dataset.weekEnd,
        source_week_start: dragged.weekStart,
        source_week_end: dragged.weekEnd,
        source_shift_id: dragged.shiftId,
        source_day_type: dragged.dayType,
        source_date: dragged.date,
        target_dates: zone.dataset.categoryDates || ""
      };
      var swapped = false;
      try {
        var moveResult;
        try {
          moveResult = await postAction(movePayload);
        } catch (error) {
          if (!error.payload || !error.payload.swap_required) throw error;
          var swapStaffIds = await promptForSwap(error.payload);
          if (!swapStaffIds) return;
          moveResult = await postAction(Object.assign({}, movePayload, { swap_staff_ids: swapStaffIds }));
          swapped = true;
        }
        notify(swapped ? "Weekly crews swapped. Refreshing the roster…" : (moveResult.message || movedStaffCount + " selected staff moved. Refreshing the roster…"), false);
        window.setTimeout(function () { window.location.reload(); }, 500);
      } catch (error) { notify(error.message, true); }
    });

    if (form) {
      document.getElementById("sc-roster-dialog-date").addEventListener("change", updateSections);
      document.getElementById("sc-roster-dialog-section").addEventListener("change", updateUsers);
      document.getElementById("sc-roster-staff-fill-slots").addEventListener("click", function () {
        var staffSelect = document.getElementById("sc-roster-dialog-staff");
        var selectedCount = staffSelect.selectedOptions.length;
        var remaining = Math.max(0, maxSelectableStaff - selectedCount);
        Array.from(staffSelect.options).some(function (option) {
          if (option.selected) return false;
          if (remaining < 1) return true;
          option.selected = true;
          remaining -= 1;
          return false;
        });
        enforceStaffSelectionLimit();
      });
      document.getElementById("sc-roster-staff-clear").addEventListener("click", function () {
        Array.from(document.getElementById("sc-roster-dialog-staff").options).forEach(function (option) {
          option.selected = false;
        });
        enforceStaffSelectionLimit();
      });
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        var submit = form.querySelector('[type="submit"]');
        var staffIds = Array.from(document.getElementById("sc-roster-dialog-staff").selectedOptions)
          .map(function (option) { return option.value; });
        if (!staffIds.length || staffIds.length > maxSelectableStaff) {
          enforceStaffSelectionLimit();
          return;
        }
        submit.disabled = true;
        var originalSubmitHtml = submit.innerHTML;
        submit.innerHTML = '<i class="fas fa-spinner fa-spin" aria-hidden="true"></i> <span>Adding ' + staffIds.length + ' staff…</span>';
        document.getElementById("sc-roster-dialog-status").textContent = "Saving " + staffIds.length + " staff…";
        try {
          var result = await postAction({
            action: "add",
            date: document.getElementById("sc-roster-dialog-date").value,
            shift_id: document.getElementById("sc-roster-dialog-shift").value,
            plant_id: document.getElementById("sc-roster-dialog-plant-id").value,
            day_type: document.getElementById("sc-roster-dialog-day-type").value,
            week_start: document.getElementById("sc-roster-dialog-week-start").value,
            week_end: document.getElementById("sc-roster-dialog-week-end").value,
            section_id: document.getElementById("sc-roster-dialog-section").value,
            staff_ids: staffIds
          });
          var applied = applyAddedAssignments(result);
          dialog.close();
          if (!applied && result.assignments && result.assignments.length) {
            notify("Weekly shift team updated. Reloading the roster view…", false);
            window.setTimeout(function () { window.location.reload(); }, 350);
          } else {
            notify(result.message || "Weekly shift team updated for " + staffIds.length + " selected staff.", false);
          }
        } catch (error) {
          document.getElementById("sc-roster-dialog-status").textContent = error.message;
        } finally {
          submit.innerHTML = originalSubmitHtml;
          submit.disabled = false;
        }
      });
      dialog.addEventListener("click", function (event) {
        if (event.target === dialog || event.target.closest("[data-close-roster-dialog]")) {
          addOptionsRequestId += 1;
          dialog.close();
        }
      });
      dialog.addEventListener("cancel", function () { addOptionsRequestId += 1; });
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
