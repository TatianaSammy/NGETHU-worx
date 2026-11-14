(function () {
  "use strict";

  function initialize(form) {
    var plant = form.querySelector('[name="treatment_plant"]');
    var shift = form.querySelector('[name="shift"]');
    var section = form.querySelector('[name="section"]');
    var user = form.querySelector('[name="user"]');
    if (!plant || !shift || !section || !user) return;

    var endpoint = plant.dataset.supervisorOptionsUrl;
    var status = document.createElement("p");
    status.className = "help";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    shift.closest(".form-row").appendChild(status);

    function setOptions(select, items, placeholder, selectedValue) {
      var valueToKeep = selectedValue === undefined ? select.value : String(selectedValue || "");
      select.replaceChildren(new Option(placeholder, ""));
      items.forEach(function (item) {
        select.add(new Option(item.label, String(item.id)));
      });
      select.value = items.some(function (item) { return String(item.id) === valueToKeep; }) ? valueToKeep : "";
      select.disabled = items.length === 0;
      if (window.django && window.django.jQuery) {
        window.django.jQuery(select).trigger("change.select2");
      }
    }

    async function loadOptions(preserve) {
      var plantId = plant.value;
      var shiftId = preserve ? shift.value : "";
      var sectionId = preserve ? section.value : "";
      var userId = preserve ? user.value : "";
      if (!plantId) {
        setOptions(shift, [], "Select a treatment plant first", "");
        setOptions(section, [], "Select a shift first", "");
        setOptions(user, [], "Select a section first", "");
        status.textContent = "Choose a treatment plant, shift, and configured section to load eligible supervisors.";
        return;
      }

      status.textContent = "Loading shifts, configured sections, and eligible users…";
      var url = new URL(endpoint, window.location.origin);
      url.searchParams.set("plant_id", plantId);
      if (shiftId) url.searchParams.set("shift_id", shiftId);
      if (sectionId) url.searchParams.set("section_id", sectionId);

      try {
        var response = await fetch(url, {
          credentials: "same-origin",
          headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" }
        });
        var result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.message || "Unable to load supervisor options.");

        setOptions(shift, result.shifts || [], "Select a shift", shiftId);
        setOptions(section, result.sections || [], shift.value ? "Select a configured section" : "Select a shift first", sectionId);
        setOptions(user, result.users || [], section.value ? "Select a staff member" : "Select a section first", userId);
        status.textContent = !result.shifts.length
          ? "No active shifts with configured section requirements are available for this plant."
          : !shift.value
            ? "Select a shift to load its configured sections."
            : !result.sections.length
              ? "This shift has no active section requirement in the selected plant."
              : !section.value
                ? "Select a section to load its staff members."
                : result.users.length + " eligible staff member(s) in this section.";
      } catch (error) {
        setOptions(shift, [], "Unable to load shifts", "");
        setOptions(section, [], "Select a shift first", "");
        setOptions(user, [], "Select a section first", "");
        status.textContent = error.message;
      }
    }

    plant.addEventListener("change", function () { loadOptions(false); });
    shift.addEventListener("change", function () {
      setOptions(section, [], "Loading configured sections…", "");
      setOptions(user, [], "Select a section first", "");
      loadOptions(true);
    });
    section.addEventListener("change", function () {
      setOptions(user, [], "Loading section staff…", "");
      loadOptions(true);
    });
    loadOptions(true);
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form").forEach(initialize);
  });
})();
