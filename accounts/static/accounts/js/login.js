document.addEventListener('DOMContentLoaded', () => {
    const usernameInput = document.getElementById('username');
    const passwordInput = document.getElementById('password');
    const passwordToggle = document.getElementById('togglePassword');
    const loginButton = document.getElementById('loginBtn');
    const loginForm = document.getElementById('loginForm');
    const feedback = document.getElementById('errorMessage');
    const feedbackText = document.getElementById('feedbackText');
    const feedbackIcon = feedback?.querySelector('.feedback-icon');
    const dismissFeedback = document.getElementById('dismissFeedback');
    const capsWarning = document.getElementById('passwordCapsWarning');
    const networkStatus = document.getElementById('networkStatus');
    const networkStatusContainer = document.querySelector('.login-network-status');
    const resetMessage = document.getElementById('resetMessage');
    const forgotLink = document.getElementById('forgotPasswordLink');

    if (!usernameInput || !passwordInput || !loginButton || !loginForm) return;

    let isSubmitting = false;

    const feedbackIcons = {
        error: 'fa-circle-exclamation',
        warning: 'fa-triangle-exclamation',
        info: 'fa-circle-info'
    };

    function updateLoginButton() {
        const ready = Boolean(usernameInput.value.trim() && passwordInput.value);
        loginButton.disabled = isSubmitting || !ready;
    }

    function clearCredentials() {
        usernameInput.value = '';
        passwordInput.value = '';
        passwordInput.type = 'password';
        passwordToggle?.setAttribute('aria-pressed', 'false');
        passwordToggle?.setAttribute('aria-label', 'Show password');
        const toggleIcon = passwordToggle?.querySelector('i');
        toggleIcon?.classList.replace('fa-eye-slash', 'fa-eye');
        usernameInput.setAttribute('aria-invalid', 'false');
        passwordInput.setAttribute('aria-invalid', 'false');
        updateLoginButton();
    }

    function clearFeedback() {
        if (!feedback) return;
        feedback.hidden = true;
        feedback.classList.remove('is-error', 'is-warning', 'is-info');
        feedback.removeAttribute('role');
        feedback.setAttribute('aria-live', 'off');
        if (feedbackText) feedbackText.textContent = '';
    }

    function showFeedback(message, level = 'error') {
        if (!feedback || !feedbackText || !message) return;
        const safeLevel = Object.hasOwn(feedbackIcons, level) ? level : 'error';
        feedback.classList.remove('is-error', 'is-warning', 'is-info');
        feedback.classList.add(`is-${safeLevel}`);
        feedbackText.textContent = message;
        feedback.hidden = false;
        feedback.setAttribute('role', safeLevel === 'info' ? 'status' : 'alert');
        feedback.setAttribute('aria-live', safeLevel === 'info' ? 'polite' : 'assertive');
        feedbackIcon?.classList.remove(...Object.values(feedbackIcons));
        feedbackIcon?.classList.add(feedbackIcons[safeLevel]);
        dismissFeedback?.focus({ preventScroll: true });
    }

    function getCSRFToken() {
        const prefix = 'csrftoken=';
        const cookie = document.cookie.split(';').map(value => value.trim())
            .find(value => value.startsWith(prefix));
        return cookie ? cookie.slice(prefix.length) : '';
    }

    function updateNetworkStatus() {
        const offline = !navigator.onLine;
        if (!networkStatus || !networkStatusContainer) return;
        networkStatus.textContent = offline ? 'Offline — reconnect before signing in' : '';
        networkStatusContainer.hidden = !offline;
        networkStatusContainer.classList.toggle('is-offline', offline);
    }

    function updateCapsLock(event) {
        if (!capsWarning) return;
        capsWarning.hidden = !event.getModifierState?.('CapsLock');
    }

    clearCredentials();
    window.addEventListener('pageshow', clearCredentials);
    window.addEventListener('pagehide', clearCredentials);
    usernameInput.focus({ preventScroll: true });
    updateNetworkStatus();
    window.addEventListener('online', updateNetworkStatus);
    window.addEventListener('offline', updateNetworkStatus);

    dismissFeedback?.addEventListener('click', clearFeedback);
    usernameInput.addEventListener('input', () => {
        usernameInput.value = usernameInput.value.toUpperCase();
        usernameInput.setAttribute('aria-invalid', 'false');
        clearFeedback();
        updateLoginButton();
    });
    passwordInput.addEventListener('input', () => {
        passwordInput.setAttribute('aria-invalid', 'false');
        clearFeedback();
        updateLoginButton();
    });

    passwordInput.addEventListener('keydown', updateCapsLock);
    passwordInput.addEventListener('keyup', updateCapsLock);
    passwordInput.addEventListener('focus', updateCapsLock);
    passwordInput.addEventListener('blur', () => {
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
        const missingUsername = !username;
        const missingPassword = !password;

        usernameInput.setAttribute('aria-invalid', String(missingUsername));
        passwordInput.setAttribute('aria-invalid', String(missingPassword));

        if (missingUsername || missingPassword) {
            showFeedback('Enter both your username and password to continue.', 'warning');
            (missingUsername ? usernameInput : passwordInput).focus({ preventScroll: true });
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
                    'X-CSRFToken': getCSRFToken(),
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
                clearFeedback();
                const container = document.querySelector('.login-container');
                if (container) {
                    container.classList.add('is-transitioning');
                    container.setAttribute('aria-busy', 'true');
                }
                redirecting = true;
                window.location.assign(destination.href);
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

    if (forgotLink && resetMessage) {
        forgotLink.addEventListener('click', async event => {
            event.preventDefault();
            if (forgotLink.classList.contains('disabled-link')) return;

            const username = usernameInput.value.trim();
            if (!username) {
                usernameInput.setAttribute('aria-invalid', 'true');
                showFeedback('Enter your username before requesting a password reset.', 'warning');
                usernameInput.focus({ preventScroll: true });
                return;
            }

            forgotLink.classList.add('disabled-link');
            resetMessage.hidden = false;
            resetMessage.className = 'reset-status sending';
            resetMessage.setAttribute('role', 'status');
            resetMessage.textContent = 'Sending password reset instructions…';

            try {
                const response = await fetch(forgotLink.dataset.url, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-CSRFToken': getCSRFToken(),
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: new URLSearchParams({ username }).toString(),
                    credentials: 'same-origin'
                });
                const payload = await response.json();
                resetMessage.className = `reset-status ${payload.success ? 'success' : 'error'}`;
                resetMessage.textContent = payload.message || (payload.success
                    ? 'If the account exists, reset instructions have been sent.'
                    : 'Unable to request a reset. Please try again.');
            } catch (error) {
                resetMessage.className = 'reset-status error';
                resetMessage.textContent = 'Unable to reach the reset service. Please try again.';
            } finally {
                window.setTimeout(() => forgotLink.classList.remove('disabled-link'), 30000);
            }
        });
    }
});
