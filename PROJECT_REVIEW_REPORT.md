# Project review report

**Review date:** 2026-10-16  
**Scope:** Static review of the Django application, UI templates and assets, Docker Compose, Nginx, and Kubernetes manifests.

## Executive summary

This repository is a Django monolith organized into domain apps, rather than independently deployed microservices. The apps share one project configuration, URL router, database, and backend image. Compose runs multiple web, ASGI, and Celery processes from that image.

The highest-risk issues found are credential-bearing Kubernetes files tracked in Git, a Kubernetes `ConfigMap` containing a database credential, publicly published database and Redis ports in Compose, an HTTPS port that has no corresponding Nginx TLS listener, and a service worker that caches authenticated responses. Account and permission management endpoints also appear too permissive for ordinary authenticated users.

The repository had extensive pre-existing local changes, including migration file deletions and replacements. This report describes the current working tree; those changes were not made as part of this review. Secret file contents were not opened or reproduced.

## Implementation update

The review findings below describe the original state. The fixes in this working tree now:

- Apply the CA dashboard palette through shared theme variables and Bootstrap tokens across the core, CA, QCA, and legacy shells. Theme changes continue to sync between open tabs.
- Keep QCA result cards visible through polling and HTMX refreshes, unregister the legacy QCA worker, and serve versioned static assets without caching authenticated pages or API responses.
- Show QCA limits only for out-of-range values, use theme-aware hover/focus colors while typing, remove the inaccurate autosave claim, and disable QCA's native fullscreen prompt behavior.
- Start the CA sidebar collapsed so it expands on hover and keyboard focus. Make the shared shell scroll at small viewport heights and improve modal keyboard focus trapping.
- Require explicit model permissions across account administration and 19 domain REST API modules, scope QCA objects to assigned plants/shifts, and make the shared permission mixin fail closed when a view lacks a permission mapping. QCA reads no longer trigger automatic state transitions.
- Restrict Compose data ports to loopback, use Redis `noeviction`, pin PgBouncer to a versioned image, build the custom Nginx image with an actual TLS listener, and proxy health checks to Django readiness.
- Remove database credentials from the Kubernetes ConfigMap, change allowed hosts and cookie settings, add ingress TLS configuration, check database/cache readiness, and use one backend tag across web, Daphne, and Celery workloads.
- Stop tracking local Kubernetes secret and PgBouncer userlist files while retaining them in the working copy; add a placeholder-only secret template. Use the repository's local Font Awesome and Plotly assets, and use integrity-pinned Bootstrap CDN assets.

### Deployment and verification items still requiring operator input

- Rotate the database password, Django secret key, and PgBouncer credentials if the values previously committed were real. Untracking them does not erase Git history. Provision the Kubernetes `qad-secrets` object from a secret manager and create the `qad-tls` certificate secret. Set the actual production hostname consistently in the ConfigMap and ingress before deployment.
- Replace the shared `qad-backend:stable` tag with the same immutable image digest for every Kubernetes workload once CI publishes that digest. PgBouncer is pinned to `v1.25.2-p0`; validate that version with the deployment's PostgreSQL and driver settings.
- The app-by-app review was static. The shared permission layer and QCA scopes were tightened, but each domain app's workflow-specific roles, exports, and approval rules still need owner review and authorization tests. Tests and live browser checks were not run in this implementation pass. The review also did not establish backups, retention, observability, or production certificate provisioning.

The secret manifest deletions are staged in the nested `qad` repository; all other changes remain in the working tree. Existing staged migration changes were preserved.

## Initial findings by priority

### P0/P1: Credential exposure and external service ports

- `k8s/configmap.yaml` is tracked and contains a `DATABASE_URL` with an embedded database credential. A ConfigMap is not a secret store. `k8s/secrets.yaml` and `k8s/userlist.txt` are also tracked according to Git's index. Treat any real credentials in these files as exposed: rotate them, remove credentials from repository history where appropriate, and use Kubernetes Secrets backed by a managed secret store. Keep only templates or key names in source control.
- Compose publishes PostgreSQL (`5432`), PgBouncer (`6432`), and Redis (`6379`) to host ports in `docker-compose.yml`. Redis is configured without an authentication option in its command. Bind development-only ports to loopback and keep production data services on private networks with authentication and firewall rules.
- `k8s/configmap.yaml` sets `ALLOWED_HOSTS` to `*`. Set the real application hostnames and configure matching ingress hosts. Wildcard host validation weakens protection against Host-header abuse.

### P1: HTTPS is not configured consistently

- Compose publishes host port `443` to container port `443` (`docker-compose.yml:611-613`), while the mounted `nginx/conf.d/default.conf` listens on port `80` only (`nginx/conf.d/default.conf:29-30`). Compose uses the stock `nginx:1.27-alpine` image (`docker-compose.yml:596`), so the custom `Dockerfile.nginx` entrypoint that renders the TLS template is not used by this service. HTTPS requests will not reach a TLS listener in this configuration.
- The Kubernetes ingress has an HTTP rule but no `tls` section (`k8s/ingress.yaml`). Configure a certificate and TLS secret or certificate controller, then verify redirects and Django's secure cookie settings end to end.
- Django's secure redirect and secure cookie settings default to `False` (`qad/settings.py:533-537`). Require secure production values at startup and enable HSTS after HTTPS is verified.

### P1: Authenticated users can manage other accounts and permissions

`accounts/views_user.py:34-52` exposes a broad user queryset to any authenticated user. The detail actions at lines 79-109 allow setting a password and uploading a picture for the selected user, and `profile_info` returns contact details. The Group and Permission viewsets at lines 158-171 also grant CRUD to any authenticated user. Apply explicit staff/role permissions, scope querysets, and prevent ordinary users from changing another user's credentials or profile.

The same file defines async handlers and async `perform_create`/`perform_update` hooks on standard DRF ViewSets (`lines 62-80, 98-117, 148-152`). Verify these against the synchronous DRF dispatch path; move the actions to supported synchronous handlers or a deliberately async-compatible API layer, and add endpoint tests.

### Resolved: Service worker offline data retention

The PWA now uses a network-only service worker. It does not cache or replay requests, and activation removes legacy QAD caches. The app requires an active connection.

### P1/P2: Redis eviction can drop queued work

Compose configures Redis with `allkeys-lru` (`docker-compose.yml:227-237`). The Django settings use Redis for cache and Channels, and the deployment also uses Redis for Celery. Under memory pressure, `allkeys-lru` can evict queue, channel, or result keys along with cache data. Separate cache from broker/channel traffic or use a dedicated broker with a queue-safe eviction policy, capacity monitoring, and persistence appropriate to the delivery guarantees.

### P2: Health checks can report success while the app is down

Nginx's `/health/` handler returns `200` directly (`nginx/conf.d/default.conf:29-33`) instead of checking an upstream. The Compose Nginx health check therefore cannot detect unavailable Django or Daphne instances. Django's `/health/` route also returns a constant response (`qad/urls.py:89`). Add separate liveness and readiness checks; readiness should verify essential dependencies such as the database and broker, with timeouts and a small, bounded check cost.

### P2: Runtime image and settings drift

- Kubernetes web uses `qad-backend:stable` (`k8s/web.yaml`), while Celery Beat uses `qad-backend:latest` (`k8s/beat-deployment.yaml`). Pin all workloads to the same immutable build digest so web and scheduled tasks cannot run different application versions.
- Compose sets `DJANGO_DEBUG=False` (`docker-compose.yml:20`), but Django reads `DEBUG` (`qad/settings.py:85`). The Dockerfile and Kubernetes ConfigMap set `DEBUG` correctly, but the Compose variable name is misleading and does not override `DEBUG` if supplied by `.env`. Standardize on one setting name and reject debug mode in production.
- PgBouncer uses the floating `latest` tag (`docker-compose.yml:172`). Pin a tested image version or digest.
- The database setting now requires a valid PostgreSQL `DATABASE_URL` in every environment and fails startup when it is missing or uses another database scheme.

## UI and UX review

### P1: Restore pinch zoom on the QC results interface

`qc/templates/qc/results/base.html:11` sets `maximum-scale=1` and `user-scalable=no`. This prevents users with low vision from zooming the interface. Remove those viewport restrictions and validate that the results grid remains usable at 200% zoom and narrow widths.

### P1/P2: Protect keyboard focus and modal navigation

- QC dashboard CSS removes focus outlines from controls (`qc/static/qc/results/css/dashboard_v2.css:217,351,822`). Some controls may have other focus styling, but keyboard focus needs a consistent, high-contrast `:focus-visible` treatment across themes.
- The shared modal script moves focus into and back out of a modal, but does not trap Tab navigation inside the open modal (`core/static/core/js/modal.js:25-49`). Implement a focus trap, announce dialog semantics, and keep background content out of the tab order while a modal is open.
- Preserve reduced-motion support across all domain apps; shared CSS has it, but individual app styles should be checked for animations that bypass it.

### P2: Make the shared shell scroll reliably on small screens

The core shell uses a fixed-height viewport and hidden overflow for `.content-wrapper` (`core/static/core/css/sidebar.css:29-46`). This can make content difficult to reach on short screens, mobile browser chrome changes, or when zoomed. Prefer a flexible `min-height` layout using dynamic viewport units where needed, and make the main content the clear scroll container. Validate forms, dashboards, and wide tables on phone-sized screens.

### P2: Reduce cross-app UI differences and duplicated navigation

The shared core shell (`core/templates/core/base/base.html`) and QC results shell (`qc/templates/qc/results/base.html`) load separate sets of styles and scripts, while multiple domain apps maintain their own header/sidebar partials. This creates inconsistent navigation, control behavior, and accessibility fixes. Consolidate common layout, typography, buttons, forms, alerts, and navigation components; let app pages provide domain content and a small set of documented variants.

The shared shell also downloads Bootstrap, Font Awesome, and three font families from external CDNs (`core/templates/core/base/base.html`). This adds third-party availability and privacy dependencies to core workflows. Self-host pinned assets or use a controlled asset pipeline; if CDNs remain, add integrity/version controls and a restrictive Content Security Policy.

### P2: Improve operational clarity in live dashboards

QC's header labels the WebSocket state “Live Stream” (`qc/templates/qc/results/components/header.html:49-53`). Ensure the label changes to reconnecting/offline states and that stale readings have a visible timestamp. For laboratory and operations screens, distinguish loading, empty, stale, and error states; provide confirmation and undo/recovery paths for destructive or irreversible workflow actions.

## App-by-app review plan

The domain folders include accounts, alerts, audit logs, analysis methods, chemical analysis (`ca`), water quality (`qc` and `qc_analytics`), laboratory, instruments, inventory, maintenance and incidents, reports, workflows, and the water-treatment analysis modules (`PHadjustment`, `chlorinedemand`, `jartest`, `microbial`, `heavy_metal`, `filtered_water`, `backwashing`, `xmicalconcentration`, `wsp`, `sc`, `wc`, `o`, `kstw`, and `destw`). They are app modules in this Django service, so the shared risks above apply across them.

For every app, use a consistent checklist:

1. Verify login, role, plant, and section authorization on every view, API action, export, and WebSocket consumer.
2. Validate status transitions and approval authority in the service/domain layer; use database transactions and constraints for important invariants.
3. Bound list and dashboard queries with pagination, indexes, and query-count checks; check for N+1 access and unbounded exports.
4. Make Celery side effects idempotent, retryable, observable, and safe under duplicate delivery.
5. Add tests for permissions, plant isolation, workflow transitions, edge cases, and migrations.
6. Use the same accessible shared UI components, responsive behavior, and clear empty/loading/error states.

## Repository and release hygiene

- The project working tree already contains significant local edits, including migration deletions/recreations. Review those diffs and the target database migration history before deploying.
- Git tracks `k8s/secrets.yaml` and `k8s/userlist.txt`; secret-like files should not be committed, even if encrypted or base64 encoded. Rotate credentials and remove the files from history where needed.
- The outer workspace also appears to contain a staged Python virtual environment. Keep environments, local databases, media, and generated assets outside version control; verify the nested repository boundary before committing.
- The repository has many `tests.py` files, but this review did not run tests or establish coverage. Add CI checks for Django system checks, migration drift, authorization, accessibility, and deployment manifest validation.

## Suggested order of work

1. Rotate exposed credentials; remove credentials from the ConfigMap and tracked secret files; restrict published service ports.
2. Fix HTTPS and host validation in Compose and Kubernetes; standardize production settings and image versions.
3. Fix user/permission endpoint authorization and the async DRF handlers.
4. Stop caching authenticated service-worker responses and improve readiness checks.
5. Restore zoom and keyboard access, then consolidate shared UI patterns and validate mobile layouts.
6. Establish app-level permission/workflow tests and deployment checks before releasing migrations.
