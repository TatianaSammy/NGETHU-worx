/**
 * Universal CRUD & Import Handler for Accounts App
 */
document.addEventListener("DOMContentLoaded", function () {
    // 1. Universal Form Modal Loader (Create / Edit)
    document.addEventListener("click", function (e) {
        var trigger = e.target.closest("[data-open-form-modal]");
        if (!trigger) return;
        e.preventDefault();

        var url = trigger.getAttribute("data-url") || trigger.getAttribute("href");
        var title = trigger.getAttribute("data-title") || "Manage Record";
        var modalEl = document.getElementById("universalFormModal");
        if (!modalEl || !url) return;

        var titleEl = modalEl.querySelector("#universalFormModalTitle span");
        var bodyEl = modalEl.querySelector("#universalFormModalBody");
        if (titleEl) titleEl.textContent = title;
        if (bodyEl) {
            bodyEl.innerHTML = '<div class="text-center py-5"><div class="spinner-border text-primary" role="status"><span class="visually-hidden">Loading...</span></div></div>';
        }

        var modal = bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.show();

        fetch(url, {
            headers: { "X-Requested-With": "XMLHttpRequest" }
        })
        .then(function (res) { return res.text(); })
        .then(function (html) {
            bodyEl.innerHTML = html;
            // Intercept form submission inside modal
            var form = bodyEl.querySelector("form");
            if (form) {
                form.addEventListener("submit", function (ev) {
                    ev.preventDefault();
                    var formData = new FormData(form);
                    fetch(form.action || url, {
                        method: "POST",
                        body: formData,
                        headers: { "X-Requested-With": "XMLHttpRequest" }
                    })
                    .then(function (response) {
                        return response.text();
                    })
                    .then(function (result) {
                        if (result.trim() === "SUCCESS" || result.indexOf("SUCCESS") !== -1) {
                            modal.hide();
                            // Reload active tab or page
                            var activePane = document.querySelector(".tab-pane.active[data-dashboard-fragment]");
                            if (activePane) {
                                var retryBtn = activePane.querySelector("[data-dashboard-retry]");
                                window.location.reload();
                            } else {
                                window.location.reload();
                            }
                        } else {
                            bodyEl.innerHTML = result;
                        }
                    })
                    .catch(function (err) {
                        alert("Submission error: " + err.message);
                    });
                });
            }
        })
        .catch(function (err) {
            bodyEl.innerHTML = '<div class="alert alert-danger">Error loading form: ' + err.message + '</div>';
        });
    });

    // 2. Universal Delete Confirmation Modal
    document.addEventListener("click", function (e) {
        var trigger = e.target.closest("[data-open-delete-modal]");
        if (!trigger) return;
        e.preventDefault();

        var url = trigger.getAttribute("data-url") || trigger.getAttribute("href");
        var name = trigger.getAttribute("data-name") || "this record";
        var modalEl = document.getElementById("universalDeleteModal");
        if (!modalEl || !url) return;

        var nameEl = document.getElementById("universalDeleteObjectName");
        var formEl = document.getElementById("universalDeleteForm");
        if (nameEl) nameEl.textContent = name;
        if (formEl) formEl.setAttribute("action", url);

        var modal = bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.show();
    });

    // 3. Universal Import Modal
    document.addEventListener("click", function (e) {
        var trigger = e.target.closest("[data-open-import-modal]");
        if (!trigger) return;
        e.preventDefault();

        var resource = trigger.getAttribute("data-resource") || "users";
        var title = trigger.getAttribute("data-title") || ("Import " + resource.charAt(0).toUpperCase() + resource.slice(1));
        var modalEl = document.getElementById("universalImportModal");
        if (!modalEl) return;

        var titleEl = document.getElementById("importModalResourceName");
        var formEl = document.getElementById("universalImportForm");
        var sampleBtn = document.getElementById("importSampleDownloadBtn");
        var feedback = document.getElementById("importFeedback");

        if (titleEl) titleEl.textContent = title;
        if (formEl) formEl.setAttribute("action", "/accounts/admin/import/" + resource + "/");
        if (sampleBtn) sampleBtn.setAttribute("href", "/accounts/admin/export-sample/" + resource + "/");
        if (feedback) {
            feedback.className = "alert d-none mt-3 mb-0";
            feedback.textContent = "";
        }

        var modal = bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.show();
    });
});

