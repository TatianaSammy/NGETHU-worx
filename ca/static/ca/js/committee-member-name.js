document.addEventListener("DOMContentLoaded", () => {
  const userSelect = document.getElementById("id_user");
  const fullNameField = document.getElementById("id_member_full_name");

  if (!userSelect || !fullNameField) return;

  const updateFullName = () => {
    const selectedOption = userSelect.selectedOptions[0];
    fullNameField.value = selectedOption?.dataset.fullName || "";
  };

  userSelect.addEventListener("change", updateFullName);
  updateFullName();
});
