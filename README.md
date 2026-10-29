# NCWSC Water Quality Assurance & Laboratory Information Management System

**waterLAB / QAD** is a web-based laboratory information management and water-quality operations platform for Nairobi City Water & Sewerage Company (NCWSC). It brings laboratory sampling, analysis, compliance monitoring, operational workflows, and staff notifications into one Django application.

## What the system does

- **Quality control and laboratory analysis:** hourly sampling worksheets for raw, clarified, and treated water; parameter validation; compliance calculations; reports and exports.
- **Chemical and treatment operations:** jar testing, chlorine demand, pH adjustment, dosing calculations, chemical batches, and treatment dashboards.
- **Shift and work management:** rosters, supervisor assignments, handovers, maintenance and incident workflows, weekly duties, and task assignments.
- **Alerts and notifications:** persisted alerts, recipient inboxes, WebSocket updates, email and configured channel delivery, retries, acknowledgement timers, and escalation policies.
- **Voice alerts:** generated alert audio can use a configured reference voice runtime; other TTS providers are fallbacks. Browser controls allow users to replay audio when autoplay is blocked.
- **Operational dashboards:** dashboards for plant operations, filtered water, quality results, non-compliance, reporting, and workload.
- **Fullscreen and PWA support:** installable application experience for laboratory and kiosk workstations.

## Technology

- Python 3.11 and Django
- PostgreSQL for production; SQLite is supported for local development
- Redis for caching, Channels/WebSockets, and Celery
- Gunicorn for HTTP, Daphne for ASGI/WebSocket traffic, and Celery workers plus beat
- Nginx for the Docker Compose front end
- Optional Kubernetes manifests under k8s/

## Repository layout

| Path | Purpose |
| --- | --- |
| qad/ | Django project settings, URL routing, ASGI and WSGI entry points |
| accounts/ | Authentication, user-facing account pages, and notification views |
| alerts/ | Alert rules, notification persistence, delivery, escalation, and voice alerts |
| qc/, ca/, PHadjustment/, chlorinedemand/, jartest/ | Quality control, chemical analysis, pH adjustment, chlorine demand, and jar testing |
| sc/, weeklytasks/, maintenance/, Maintenance_Incident/ | Shift, duty, maintenance, and incident workflows |
| core/ | Shared models, middleware, templates, styling, PWA, and browser notification code |
| t/, company/, CompanyInfo/ | Treatment plant and organization configuration |
| filtered_water/, o/, destw/, wsp/, wc/ | Operational and treatment dashboards and modules |
| k8s/ | Kubernetes manifests and a secrets template |
| nginx/, pgbouncer/ | Reverse proxy and PostgreSQL connection-pool configuration |
| Dockerfile, Dockerfile.nginx, docker-compose.yml | Container builds and Docker Compose deployment |

## Requirements

### Local development

- Python 3.11
- Redis 7 for background tasks and real-time notifications
- PostgreSQL only if developing against PostgreSQL; the development configuration can use SQLite

### Docker Compose deployment

- Docker Engine or Docker Desktop with the Compose v2 plugin
- A .env file containing the production settings described below
- Persistent disk space for PostgreSQL, uploaded media, logs, and collected static files

### Kubernetes deployment

- A working Kubernetes cluster, kubectl, and a StorageClass that can provision the PVCs in k8s/pvc.yaml
- An ingress controller and TLS secret if publishing through HTTPS
- A container registry for clusters that cannot load a locally built image

## Local development

Run these commands from the repository root.

### 1. Create and activate a virtual environment

Windows PowerShell:

~~~powershell
py -3.11 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
~~~

macOS or Linux:

~~~bash
python3.11 -m venv .venv
source .venv/bin/activate
~~~

### 2. Install Python dependencies

~~~bash
python -m pip install --upgrade pip
pip install -r requirements.txt
~~~

### 3. Configure the development environment

The Django settings load a root .env file. Create one with a fresh secret key:

~~~env
SECRET_KEY=replace-with-a-long-random-value
DEBUG=True
ENVIRONMENT=development
ALLOWED_HOSTS=localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=http://localhost,http://127.0.0.1
REDIS_URL=redis://127.0.0.1:6379/0
~~~

Do not reuse the example value or commit .env. For a local secret key, generate a value with:

~~~bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
~~~

When DEBUG=True and no DATABASE_URL is provided, the development configuration uses db.sqlite3. Start Redis locally if you need Channels, Celery, and live notifications.

### 4. Initialize and run the app

~~~bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
~~~

Open http://127.0.0.1:8000/. To run the ASGI server for WebSockets instead of Django's development server:

~~~bash
daphne -b 127.0.0.1 -p 8000 qad.asgi:application
~~~

Run background tasks in separate terminals:

~~~bash
celery -A qad worker --loglevel=INFO --concurrency=2
celery -A qad beat --loglevel=INFO --scheduler django_celery_beat.schedulers:DatabaseScheduler
~~~

For production static assets, run python manage.py collectstatic --noinput; the Compose migration service does this during deployment.

## Docker Compose deployment

The Compose stack builds the Django image from Dockerfile and starts PostgreSQL, PgBouncer, Redis, a migration/static-file service, four HTTP workers, three Daphne workers, four Celery workers, Celery beat, and Nginx. Nginx publishes HTTP on host port 8000 and HTTPS on 443. The database, PgBouncer, and Redis host ports bind to loopback by default.

### 1. Create the deployment environment file

Create .env in the repository root. Use unique production secrets; keep this file out of Git:

~~~env
SECRET_KEY=replace-with-a-unique-long-random-value
DEBUG=False
ENVIRONMENT=production
DJANGO_SETTINGS_MODULE=qad.settings
ALLOWED_HOSTS=your-domain.example
CSRF_TRUSTED_ORIGINS=https://your-domain.example

POSTGRES_DB=qad
POSTGRES_USER=qad
POSTGRES_PASSWORD=replace-with-a-unique-database-password
POSTGRES_HOST=db
POSTGRES_PORT=5432
DATABASE_URL=postgresql://qad:replace-with-a-unique-database-password@pgbouncer:6432/qad

REDIS_HOST=redis
REDIS_PORT=6379
REDIS_URL=redis://redis:6379/0
REDIS_CACHE_URL=redis://redis:6379/2
REDIS_CHANNEL_URL=redis://redis:6379/3
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1

TZ=Africa/Nairobi
NGINX_HOST=your-domain.example
~~~

If your database password contains URL-reserved characters, URL-encode it in DATABASE_URL. Set the same database username/password in pgbouncer/userlist.txt using the PgBouncer format documented in that file. Configure mail, external notification providers, Sentry, and TLS according to your deployment.

Update ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, and Nginx's host/TLS configuration for the real public hostname. The included ports and database tuning are examples; review them against your host capacity and network policy before production use.

### 2. Build and start the services

~~~bash
docker compose config
docker compose up -d --build
docker compose ps
~~~

The migrate service applies Django migrations and runs collectstatic before application workers start. Create an administrator:

~~~bash
docker compose exec web1 python manage.py createsuperuser
~~~

View logs and service health:

~~~bash
docker compose logs -f web1 daphne1 celery1 beat nginx
docker compose ps
~~~

The site is served through Nginx at http://localhost:8000/. HTTPS on port 443 requires valid certificates and corresponding Nginx configuration.

### 3. Stop or update the stack

~~~bash
docker compose down
docker compose up -d --build
~~~

docker compose down keeps named data volumes. Use docker compose down -v only when you intentionally want to delete the database, media, logs, and other persistent Compose data.

### 4. Back up the database

~~~bash
docker compose exec -T db pg_dump -U qad -d qad > backup.sql
~~~

Protect database backups and uploaded media as sensitive operational data. Test restoration procedures before relying on a backup.

## Kubernetes deployment

The manifests in k8s/ are deployment building blocks, not a turnkey cluster configuration. They expect an image named qad-backend:stable, namespace qad, a qad-config ConfigMap, a qad-secrets Secret, and persistent volumes. Configure the cluster's ingress, TLS, storage, and resource sizing before exposing the application.

The older deploy-qad.ps1 helper is not ready to run as-is: it references k8s/qad-ha.yaml, which is not present in this checkout, and it runs docker system prune -a --volumes -f. That command removes Docker images and volumes beyond this project. Use the ordered steps below and review any automation script before running it.

### 1. Build and make the application image available

For a local Kind cluster:

~~~bash
docker build -t qad-backend:stable .
kind create cluster --name qad
kind load docker-image qad-backend:stable --name qad
~~~

For another cluster, tag and push the image to a registry accessible by its nodes, then update the image fields in the workload manifests and use an appropriate imagePullPolicy.

### 2. Configure cluster settings and secrets

Review k8s/configmap.yaml and change hostnames, trusted origins, time zone, and other non-secret settings for the target environment.

Copy k8s/secrets.example.yaml to a local k8s/secrets.yaml, replace every placeholder with real values, and keep the resulting file out of Git. It contains the Django SECRET_KEY, PostgreSQL password, database URL, and PgBouncer user list. Create the TLS secret referenced by k8s/ingress.yaml using your certificate provider, or adjust/remove the Ingress resource if TLS terminates elsewhere.

### 3. Apply manifests in dependency order

~~~bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secrets.yaml
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/redis.yaml
kubectl apply -f k8s/pgbouncer.yaml
kubectl apply -f k8s/web.yaml
kubectl apply -f k8s/daphne-deployment.yaml
kubectl apply -f k8s/celery-deployment.yaml
kubectl apply -f k8s/beat-deployment.yaml
kubectl apply -f k8s/ingress.yaml
kubectl apply -f k8s/hpa.yaml
~~~

The PVCs require a compatible StorageClass. The HPA requires the cluster metrics API. The Ingress requires an installed ingress controller and the referenced TLS Secret.

### 4. Check the rollout

~~~bash
kubectl get pods,services,pvc -n qad
kubectl rollout status deployment/qad-web -n qad
kubectl rollout status deployment/qad-daphne -n qad
kubectl rollout status deployment/qad-celery -n qad
kubectl rollout status deployment/qad-beat -n qad
kubectl logs -n qad deployment/qad-web
~~~

Update or roll back deployments using the normal Kubernetes rollout commands after publishing a new image.

## Real-time alerts and voice

Alert events and recipient delivery state are persisted before delivery. Redis and Daphne support the live WebSocket inbox; Celery workers handle background delivery, voice generation, retry, and escalation tasks; Celery beat schedules periodic work. Configure external delivery providers and email credentials through environment variables or a secret manager.

Reference voice cloning is optional. The worker lazily loads a compatible Chatterbox Turbo/Nano runtime when installed. The pinned NumPy and Transformers versions may conflict with upstream voice packages, so validate a compatible runtime in a separate worker image before enabling it. Without that runtime, supported TTS providers can produce non-cloned audio. Browser autoplay may be blocked; users can replay from the notification Voice control.

## Fullscreen and PWA use

The app remembers fullscreen state across internal navigation and provides an installable PWA experience. On a lab workstation, install the site from Chrome or Edge for an app window. For a local development server:

~~~cmd
msedge.exe --app=http://localhost:8000/qc/dashboard/ --start-fullscreen
chrome.exe --app=http://localhost:8000/qc/dashboard/ --start-fullscreen
~~~

## Operational notes

- Keep .env, k8s/secrets.yaml, private keys, database dumps, and user-uploaded media out of source control.
- Set DEBUG=False, a unique SECRET_KEY, valid ALLOWED_HOSTS, and HTTPS trusted origins in production.
- Configure TLS, backups, database retention, log rotation, monitoring, and alert delivery for the operating environment.
- Review container resource requirements before deployment. The Compose PostgreSQL service uses high-throughput tuning that may need adjustment on smaller hosts.
- The Compose docker-proxy service mounts the host Docker socket and makes the Docker API available on the Compose network. Keep it only if the application requires it, and restrict network access.
- The repository includes multiple Kubernetes YAML files for different experiments. The ordered deployment above uses the namespace-scoped manifests listed in that procedure; do not apply every file in k8s/ blindly.

## License and ownership

Proprietary software developed for Nairobi City Water & Sewerage Company (NCWSC). All rights reserved. Use and distribution require authorization from the software owner.
