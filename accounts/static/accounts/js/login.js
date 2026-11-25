document.addEventListener('DOMContentLoaded', () => {
    const usernameInput = document.getElementById('username');
    const passwordInput = document.getElementById('password');
    const passwordToggle = document.getElementById('togglePassword');
    const loginButton = document.getElementById('loginBtn');
    const loginForm = document.getElementById('loginForm');
    const feedback = document.getElementById('errorMessage');
    const feedbackText = document.getElementById('feedbackText');
    const feedbackIcon = feedback?.querySelector('.feedback-icon');
    const capsWarning = document.getElementById('passwordCapsWarning');
    const loginPasswordPolicy = document.getElementById('loginPasswordPolicy');
    const loginPolicyNotice = document.getElementById('loginPolicyNotice');
    const networkStatus = document.getElementById('networkStatus');
    const networkStatusContainer = document.querySelector('.login-network-status');
    const resetMessage = document.getElementById('resetMessage');
    const resetMessageText = document.getElementById('resetMessageText');
    const resetMessageIcon = document.getElementById('resetMessageIcon');
    const resetValidationMessage = document.getElementById('resetValidationMessage');
    const forgotLink = document.getElementById('forgotPasswordLink');
    const resetDialog = document.getElementById('forgotPasswordDialog');
    const resetRequestForm = document.getElementById('passwordResetRequestForm');
    const resetRequestUsername = document.getElementById('resetUsername');
    const resetRequestButton = document.getElementById('resetRequestSubmit');
    const resetDialogCancel = document.getElementById('closePasswordResetDialog');

    if (!usernameInput || !passwordInput || !loginButton || !loginForm) return;

    let isSubmitting = false;
    let passwordWasActivated = false;
    let feedbackTimeout = null;
    let networkStatusTimeout = null;
    let capsWarningTimeout = null;
    const minUsernameLength = Number(loginForm.dataset.minUsernameLength || usernameInput.minLength || 1);
    const maxUsernameLength = 10;
    const minPasswordLength = Math.max(8, Number(loginForm.dataset.minPasswordLength || passwordInput.minLength || 1));
    const strengthFill = loginPasswordPolicy?.querySelector('[data-strength-fill]');
    const passwordRules = [
        { name: 'length', test: value => value.length >= minPasswordLength },
        { name: 'uppercase', test: value => /\p{Lu}/u.test(value) },
        { name: 'lowercase', test: value => /\p{Ll}/u.test(value) },
        { name: 'digit', test: value => /\p{N}/u.test(value) },
        { name: 'special', test: value => /[!@#$%^&*()_+=-]/.test(value) }
    ];

    const feedbackIcons = {
        error: 'fa-circle-exclamation',
        warning: 'fa-triangle-exclamation',
        info: 'fa-circle-info',
        success: 'fa-circle-check'
    };

    const usernamePattern = (() => {
        const pattern = loginForm.dataset.usernamePattern;
        if (!pattern) return null;
        try {
            return new RegExp(pattern);
        } catch {
            return null;
        }
    })();
    const resetUsernamePattern = (() => {
        const pattern = resetRequestForm?.dataset.usernamePattern || loginForm.dataset.usernamePattern;
        if (!pattern) return null;
        try {
            return new RegExp(pattern);
        } catch {
            return null;
        }
    })();
    let resetMessageTimeout = null;
    let resetValidationTimeout = null;
    let isResetSubmitting = false;

    function isValidLoginUsername() {
        const username = usernameInput.value.trim().toUpperCase();
        return Boolean(
            usernamePattern
            && username.length >= minUsernameLength
            && username.length <= maxUsernameLength
            && usernamePattern.test(username)
        );
    }

    function updatePasswordMeter(usernameValid) {
        const visible = Boolean(usernameValid);
        if (loginPasswordPolicy) loginPasswordPolicy.hidden = !visible;
        if (!visible) {
            if (strengthFill) {
                strengthFill.style.width = '0%';
                strengthFill.classList.remove('is-complete');
            }
            return false;
        }

        const password = passwordInput.value;
        const results = passwordRules.map(rule => {
            const met = rule.test(password);
            loginPasswordPolicy?.querySelector(`[data-password-rule="${rule.name}"]`)?.classList.toggle('is-met', met);
            return met;
        });
        const score = results.filter(Boolean).length;
        if (strengthFill) {
            strengthFill.style.width = `${(score / passwordRules.length) * 100}%`;
            strengthFill.classList.toggle('is-complete', score === passwordRules.length);
        }
        const meter = loginPasswordPolicy?.querySelector('.password-strength-meter');
        meter?.setAttribute('aria-label', `Password strength: ${score} of 5 checks met`);
        return score === passwordRules.length;
    }

    function updateLoginButton() {
        const usernameValid = isValidLoginUsername();
        const passwordEnabled = usernameValid;

        usernameInput.setAttribute('aria-invalid', String(Boolean(usernameInput.value.trim()) && !usernameValid));
        if (passwordInput.disabled !== !passwordEnabled) {
            passwordInput.disabled = !passwordEnabled;
            if (!passwordEnabled) passwordInput.value = '';
        }
        if (passwordToggle) passwordToggle.disabled = !passwordEnabled;
        if (!passwordEnabled && capsWarning) capsWarning.hidden = true;

        const passwordValid = updatePasswordMeter(usernameValid);
        const ready = navigator.onLine && usernameValid && passwordValid;
        if (loginPolicyNotice) {
            const shouldShowNotice = usernameValid && !ready;
            loginPolicyNotice.classList.toggle('is-visible', shouldShowNotice);
            loginPolicyNotice.setAttribute('aria-hidden', String(!shouldShowNotice));
        }
        loginButton.disabled = isSubmitting || !ready;
    }

    function clearCredentials() {
        usernameInput.value = '';
        passwordInput.value = '';
        usernameInput.setAttribute('readonly', '');
        passwordInput.setAttribute('readonly', '');
        passwordWasActivated = false;
        passwordInput.type = 'password';
        passwordToggle?.setAttribute('aria-pressed', 'false');
        passwordToggle?.setAttribute('aria-label', 'Show password');
        const toggleIcon = passwordToggle?.querySelector('i');
        toggleIcon?.classList.replace('fa-eye-slash', 'fa-eye');
        passwordInput.setAttribute('aria-invalid', 'false');
        updateLoginButton();
    }

    function clearFeedback() {
        if (feedbackTimeout) window.clearTimeout(feedbackTimeout);
        feedbackTimeout = null;
        if (!feedback) return;
        feedback.hidden = true;
        feedback.classList.remove('is-error', 'is-warning', 'is-info', 'is-success');
        feedback.removeAttribute('role');
        feedback.setAttribute('aria-live', 'off');
        if (feedbackText) feedbackText.textContent = '';
    }

    function showFeedback(message, level = 'error') {
        if (!feedback || !feedbackText || !message) return;
        clearFeedback();
        const safeLevel = Object.hasOwn(feedbackIcons, level) ? level : 'error';
        feedback.classList.remove('is-error', 'is-warning', 'is-info', 'is-success');
        feedback.classList.add(`is-${safeLevel}`);
        feedbackText.textContent = message;
        feedback.hidden = false;
        const isPolite = safeLevel === 'info' || safeLevel === 'success';
        feedback.setAttribute('role', isPolite ? 'status' : 'alert');
        feedback.setAttribute('aria-live', isPolite ? 'polite' : 'assertive');
        feedbackIcon?.classList.remove(...Object.values(feedbackIcons));
        feedbackIcon?.classList.add(feedbackIcons[safeLevel]);
        feedbackTimeout = window.setTimeout(clearFeedback, 4000);
    }

    function clearResetMessage() {
        if (resetMessageTimeout) window.clearTimeout(resetMessageTimeout);
        resetMessageTimeout = null;
        if (!resetMessage || !resetMessageText) return;
        resetMessage.hidden = true;
        resetMessageText.textContent = '';
        resetMessage.className = 'reset-message';
        resetMessage.setAttribute('role', 'status');
        resetMessage.setAttribute('aria-live', 'polite');
        resetMessageIcon?.classList.remove('fa-circle-check', 'fa-circle-exclamation', 'fa-triangle-exclamation');
        resetMessageIcon?.classList.add('fa-circle-info');
    }

    function showResetMessage(message, level = 'error') {
        if (!resetMessage || !resetMessageText || !message) return;
        clearResetMessage();
        const safeLevel = ['success', 'info', 'error', 'warning'].includes(level) ? level : 'error';
        const visualLevel = safeLevel === 'error' || safeLevel === 'warning' ? safeLevel : 'info';
        resetMessage.className = `reset-message is-${visualLevel}`;
        resetMessageText.textContent = message;
        resetMessage.hidden = false;
        resetMessage.setAttribute('role', visualLevel === 'error' || visualLevel === 'warning' ? 'alert' : 'status');
        resetMessage.setAttribute('aria-live', visualLevel === 'error' || visualLevel === 'warning' ? 'assertive' : 'polite');
        resetMessageIcon?.classList.remove('fa-circle-info', 'fa-circle-check', 'fa-circle-exclamation', 'fa-triangle-exclamation');
        const icon = safeLevel === 'success'
            ? 'fa-circle-check'
            : visualLevel === 'warning'
                ? 'fa-triangle-exclamation'
                : visualLevel === 'error'
                    ? 'fa-circle-exclamation'
                    : 'fa-circle-info';
        resetMessageIcon?.classList.add(icon);
        resetMessageTimeout = window.setTimeout(clearResetMessage, 4000);
    }

    function clearResetValidationMessage() {
        if (resetValidationTimeout) window.clearTimeout(resetValidationTimeout);
        resetValidationTimeout = null;
        if (!resetValidationMessage) return;
        resetValidationMessage.hidden = true;
        resetValidationMessage.textContent = '';
        resetValidationMessage.className = 'reset-validation-message';
    }

    function showResetValidationMessage(message) {
        if (!resetValidationMessage || !message) return;
        clearResetValidationMessage();
        resetValidationMessage.textContent = message;
        resetValidationMessage.hidden = false;
        resetValidationTimeout = window.setTimeout(clearResetValidationMessage, 4000);
    }

    function isValidResetUsername() {
        const username = resetRequestUsername?.value.trim().toUpperCase() || '';
        return Boolean(username && username.length <= maxUsernameLength && resetUsernamePattern?.test(username));
    }

    function updateResetRequestButton() {
        if (!resetRequestUsername || !resetRequestButton) return;
        const username = resetRequestUsername.value.trim().toUpperCase();
        const valid = isValidResetUsername();
        resetRequestUsername.setAttribute('aria-invalid', String(Boolean(username) && !valid));
        resetRequestButton.disabled = !valid || isResetSubmitting;
    }

    function getCSRFToken(form = loginForm) {
        // Prefer Django's masked token rendered into this form. It remains
        // available when the CSRF cookie is HttpOnly and avoids stale cookie
        // values when a browser has old localhost cookies cached.
        const formToken = form?.querySelector('input[name="csrfmiddlewaretoken"]')?.value?.trim();
        if (formToken) return formToken;

        const prefix = 'csrftoken=';
        const cookie = document.cookie.split(';').map(value => value.trim())
            .find(value => value.startsWith(prefix));
        if (!cookie) return '';

        const token = cookie.slice(prefix.length).trim();
        try {
            return decodeURIComponent(token);
        } catch {
            return token;
        }
    }

    function updateNetworkStatus() {
        const offline = !navigator.onLine;
        if (!networkStatus || !networkStatusContainer) return;
        if (networkStatusTimeout) window.clearTimeout(networkStatusTimeout);
        networkStatusTimeout = null;
        networkStatus.textContent = offline ? 'Offline — reconnect before signing in' : '';
        networkStatusContainer.hidden = !offline;
        networkStatusContainer.classList.toggle('is-offline', offline);
        if (offline) {
            networkStatusTimeout = window.setTimeout(() => {
                if (!navigator.onLine) networkStatusContainer.hidden = true;
                networkStatusTimeout = null;
            }, 4000);
        }
        updateLoginButton();
    }

    function updateCapsLock(event) {
        if (!capsWarning) return;
        const capsIsOn = Boolean(event.getModifierState?.('CapsLock'));
        capsWarning.hidden = !capsIsOn;
        if (capsWarningTimeout) window.clearTimeout(capsWarningTimeout);
        capsWarningTimeout = capsIsOn
            ? window.setTimeout(() => {
                capsWarning.hidden = true;
                capsWarningTimeout = null;
            }, 4000)
            : null;
    }

    clearCredentials();
    if (feedback && !feedback.hidden) {
        feedbackTimeout = window.setTimeout(clearFeedback, 4000);
    }
    window.addEventListener('pageshow', clearCredentials);
    window.addEventListener('pagehide', clearCredentials);
    usernameInput.focus({ preventScroll: true });
    updateNetworkStatus();
    window.addEventListener('online', updateNetworkStatus);
    window.addEventListener('offline', updateNetworkStatus);

    usernameInput.addEventListener('focus', () => {
        usernameInput.removeAttribute('readonly');
    });

    usernameInput.addEventListener('input', () => {
        usernameInput.value = usernameInput.value.slice(0, maxUsernameLength).toUpperCase();
        passwordInput.value = '';
        passwordWasActivated = false;
        clearFeedback();
        const enteredUsername = usernameInput.value.trim();
        if (enteredUsername.length >= minUsernameLength && !isValidLoginUsername()) {
            showFeedback('Invalid username', 'error');
        }
        updateLoginButton();
    });
    passwordInput.addEventListener('input', () => {
        passwordInput.setAttribute('aria-invalid', 'false');
        clearFeedback();
        updateLoginButton();
    });

    passwordInput.addEventListener('keydown', updateCapsLock);
    passwordInput.addEventListener('keyup', updateCapsLock);
    passwordInput.addEventListener('focus', event => {
        if (!passwordWasActivated) {
            passwordInput.value = '';
            passwordInput.removeAttribute('readonly');
            passwordWasActivated = true;
            updateLoginButton();
        }
        updateCapsLock(event);
    });
    passwordInput.addEventListener('blur', () => {
        if (capsWarningTimeout) window.clearTimeout(capsWarningTimeout);
        capsWarningTimeout = null;
        if (capsWarning) capsWarning.hidden = true;
    });

    passwordToggle?.addEventListener('click', () => {
        const reveal = passwordInput.type === 'password';
        passwordInput.type = reveal ? 'text' : 'password';
        passwordToggle.setAttribute('aria-pressed', String(reveal));
        passwordToggle.setAttribute('aria-label', reveal ? 'Hide password' : 'Show password');
        const icon = passwordToggle.querySelector('i');
        icon?.classList.toggle('fa-eye', !reveal);
        icon?.classList.toggle('fa-eye-slash', reveal);
        passwordInput.focus({ preventScroll: true });
    });

    loginForm.addEventListener('submit', async event => {
        event.preventDefault();
        if (isSubmitting) return;

        const username = usernameInput.value.trim();
        const password = passwordInput.value;
        const usernameReady = isValidLoginUsername();
        const passwordReady = passwordRules.every(rule => rule.test(password));

        usernameInput.setAttribute('aria-invalid', String(!usernameReady));
        passwordInput.setAttribute('aria-invalid', String(!passwordReady));

        if (!usernameReady) {
            if (username.length >= minUsernameLength) showFeedback('Invalid username', 'error');
            updateLoginButton();
            usernameInput.focus({ preventScroll: true });
            return;
        }

        if (!passwordReady) {
            updateLoginButton();
            passwordInput.focus({ preventScroll: true });
            return;
        }

        if (!navigator.onLine) {
            showFeedback('You appear to be offline. Reconnect and try again.', 'warning');
            updateNetworkStatus();
            return;
        }

        isSubmitting = true;
        loginButton.disabled = true;
        loginButton.setAttribute('aria-busy', 'true');
        loginButton.textContent = 'Signing in…';

        const body = new URLSearchParams({ username, password });
        clearCredentials();
        const controller = new AbortController();
        const timeoutId = window.setTimeout(() => controller.abort(), 25000);
        let redirecting = false;

        try {
            const response = await fetch(loginForm.action, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'X-CSRFToken': getCSRFToken(loginForm),
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: body.toString(),
                signal: controller.signal,
                credentials: 'same-origin'
            });
            const payload = await response.json().catch(() => null);

            if (payload?.success && payload.redirect) {
                const destination = new URL(payload.redirect, window.location.origin);
                if (destination.origin !== window.location.origin) {
                    showFeedback('The sign-in destination was rejected. Contact support.', 'error');
                    return;
                }
                showFeedback('Sign-in successful. Redirecting...', 'success');
                document.body.classList.add('is-login-transitioning');
                document.getElementById('loginWrapper')?.setAttribute('aria-busy', 'true');
                redirecting = true;
                window.setTimeout(() => window.location.assign(destination.href), 750);
                return;
            }

            const message = payload?.error || (response.status >= 500
                ? 'The sign-in service is temporarily unavailable. Try again shortly.'
                : response.ok
                    ? 'Sign-in could not be completed. Try again.'
                    : 'Sign-in failed. Check your details or contact your administrator.');
            const level = payload?.severity || (response.status >= 500 ? 'warning' : 'error');
            showFeedback(message, level);
        } catch (error) {
            const message = error.name === 'AbortError'
                ? 'The sign-in request took too long. Check your connection and try again.'
                : 'Unable to reach the sign-in service. Check your connection and try again.';
            showFeedback(message, 'warning');
        } finally {
            window.clearTimeout(timeoutId);
            if (!redirecting) {
                isSubmitting = false;
                loginButton.removeAttribute('aria-busy');
                loginButton.textContent = 'Login';
                updateLoginButton();
            }
        }
    });

    if (forgotLink && resetDialog && resetRequestForm && resetRequestUsername && resetRequestButton) {
        resetRequestUsername.addEventListener('focus', () => {
            resetRequestUsername.removeAttribute('readonly');
        });

        forgotLink.addEventListener('click', event => {
            event.preventDefault();
            clearResetMessage();
            clearResetValidationMessage();
            resetRequestForm.reset();
            resetRequestUsername.setAttribute('readonly', '');
            resetRequestUsername.setAttribute('aria-invalid', 'false');
            resetRequestButton.textContent = 'Send reset email';
            isResetSubmitting = false;
            updateResetRequestButton();
            if (typeof resetDialog.showModal === 'function') {
                resetDialog.showModal();
            } else {
                resetDialog.setAttribute('open', '');
            }
            window.setTimeout(() => resetRequestUsername.focus({ preventScroll: true }), 0);
        });

        resetRequestUsername.addEventListener('input', () => {
            resetRequestUsername.value = resetRequestUsername.value.slice(0, maxUsernameLength).toUpperCase();
            clearResetValidationMessage();
            updateResetRequestButton();
        });

        resetRequestUsername.addEventListener('blur', () => {
            if (!isValidResetUsername()) {
                showResetValidationMessage('Enter a valid username in the required format.');
            }
        });

        resetDialogCancel?.addEventListener('click', () => resetDialog.close());
        resetDialog.addEventListener('close', () => {
            clearResetValidationMessage();
            resetRequestForm.reset();
            resetRequestUsername.setAttribute('readonly', '');
            resetRequestUsername.setAttribute('aria-invalid', 'false');
            updateResetRequestButton();
        });

        resetRequestForm.addEventListener('submit', async event => {
            event.preventDefault();
            if (isResetSubmitting) return;

            if (!isValidResetUsername()) {
                showResetValidationMessage('Enter a valid username in the required format.');
                resetRequestUsername.focus({ preventScroll: true });
                updateResetRequestButton();
                return;
            }

            const username = resetRequestUsername.value.trim().toUpperCase();
            isResetSubmitting = true;
            resetRequestButton.disabled = true;
            resetRequestButton.textContent = 'Sending…';
            clearResetValidationMessage();

            let responseMessage = 'Unable to request a reset. Please try again.';
            let responseLevel = 'error';
            try {
                const response = await fetch(resetRequestForm.dataset.url, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-CSRFToken': getCSRFToken(resetRequestForm),
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: new URLSearchParams({ username }).toString(),
                    credentials: 'same-origin'
                });
                const payload = await response.json().catch(() => null);
                responseMessage = payload?.message || (response.ok
                    ? 'Password reset request completed.'
                    : 'Unable to request a reset. Check the username and try again.');
                responseLevel = payload?.success && response.ok ? 'success' : 'error';
            } catch {
                responseMessage = 'Unable to reach the reset service. Please try again.';
                responseLevel = 'warning';
            } finally {
                isResetSubmitting = false;
                resetRequestButton.textContent = 'Send reset email';
                updateResetRequestButton();
            }

            if (resetDialog.open) resetDialog.close();
            showResetMessage(responseMessage, responseLevel);
        });
    }
});
