(() => {
    "use strict";

    const form = document.getElementById("incident-report-form");
    const dialog = document.getElementById("incident-report-dialog");
    if (!form || !dialog) return;

    const status = document.getElementById("incident-submit-status");
    const submitButton = form.querySelector("[type='submit']");
    const plantSelect = document.getElementById("incident-report-plant");
    const houseSelect = document.getElementById("incident-report-house");
    let isSending = false;

    function setStatus(message, isError = false) {
        if (!status) return;
        status.textContent = message;
        status.classList.toggle("is-error", isError);
    }

    function openDialog() {
        setStatus("");
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "open");
    }

    document.querySelectorAll("[data-open-incident-report]").forEach(button => {
        button.addEventListener("click", openDialog);
    });
    document.querySelectorAll("[data-close-incident-report]").forEach(button => {
        button.addEventListener("click", () => dialog.close());
    });

    if (plantSelect && houseSelect) {
        plantSelect.addEventListener("change", () => {
            if (houseSelect.selectedOptions[0]?.dataset.plant !== plantSelect.value) {
                houseSelect.value = "";
            }
        });
    }

    form.addEventListener("submit", async event => {
        event.preventDefault();
        if (isSending || !form.reportValidity()) return;
        if (!navigator.onLine) {
            setStatus("No network connection. The report was not saved; reconnect and submit again.", true);
            return;
        }

        isSending = true;
        if (submitButton) submitButton.disabled = true;
        setStatus("Submitting incident report…");
        try {
            const response = await fetch(form.action, {
                method: "POST",
                body: new FormData(form),
                credentials: "same-origin",
                cache: "no-store",
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                    "Cache-Control": "no-cache"
                }
            });
            const payload = await response.json();
            if (!response.ok || !payload.success) {
                throw new Error(payload.message || "The report could not be submitted.");
            }
            form.reset();
            setStatus(payload.message || "Incident report submitted.");
            window.setTimeout(() => dialog.close(), 900);
        } catch (error) {
            setStatus(error.message || "The report was not submitted. Reconnect and try again.", true);
        } finally {
            isSending = false;
            if (submitButton) submitButton.disabled = false;
        }
    });

    window.addEventListener("offline", () => {
        setStatus("No network connection. Reports require an active connection and are not stored locally.", true);
    });
})();
