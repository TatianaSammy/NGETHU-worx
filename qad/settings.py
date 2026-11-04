from pathlib import Path
import os
import warnings
import re
from urllib.parse import urlparse

# =========================================================
# BASE PATH
# =========================================================
BASE_DIR = Path(__file__).resolve().parent.parent

# =========================================================
# WARNINGS
# =========================================================
warnings.filterwarnings(
    "ignore",
    message=r"Found another file with the destination path.*"
)

# =========================================================
# ENV LOADING (SAFE + DOCKER FRIENDLY)
# =========================================================
ENV_PATH = BASE_DIR / ".env"
_env_pattern = re.compile(r"\$\{([^}]+)\}")


def _expand(value: str) -> str:
    if not isinstance(value, str):
        return value
    return _env_pattern.sub(lambda m: os.getenv(m.group(1), ""), value)


def load_env_file():
    if not ENV_PATH.exists():
        return

    try:
        with ENV_PATH.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()

                if not line or line.startswith("#") or "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = _expand(value.strip())

                if key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


load_env_file()

# =========================================================
# ENV HELPER
# =========================================================
def env(key, default=None, cast=str):
    val = os.getenv(key, default)

    if val is None:
        return default

    try:
        if cast is bool:
            return str(val).lower() in ("1", "true", "yes", "y", "on")
        if cast is int:
            return int(val)
        if cast is float:
            return float(val)
        return cast(val)
    except Exception:
        return default


# =========================================================
# CORE SETTINGS
# =========================================================
SECRET_KEY = env("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is required")

DEBUG = env("DEBUG", "False", bool)
ENVIRONMENT = env("ENVIRONMENT", "DEV")
TIME_ZONE = env("TZ", "Africa/Nairobi")

ALLOWED_HOSTS = [
    h.strip()
    for h in env("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]


# =========================================================
# APPLICATIONS
# =========================================================
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django.contrib.postgres",

    # third-party
    "channels",
    "rest_framework",
    "nested_admin",
    "viewflow",
    "viewflow.workflow",
    "django_celery_beat",
    "django_celery_results",
    "rangefilter",
    "crispy_forms",
    "axes",

    # local apps
    "core",
    "accounts.apps.AccountsConfig",
    "parameters",
    "t",

    "CompanyInfo",
    "laboratory",
    "sc.apps.ScConfig",
    "weeklytasks.apps.WeeklytasksConfig",
    "wsp",
    "instruments",
    "alerts",
    "ca",
    'audit_logs',
    "o",
    "PHadjustment",  "chlorinedemand", "jartest",
    "kstw.apps.KstwConfig", "destw.apps.DestwConfig",
    "analysis_methods",
    'duty_house', 'maintenance.apps.MaintenanceConfig', 'Maintenance_Incident',
    "itinerary",
    "heavy_metal.apps.HeavyMetalConfig", "microbial.apps.MicrobialConfig", "filtered_water","qc", 'qc_analytics','wc',
    "xmicalconcentration","backwashing",
    "inventory",
    "central_lab.apps.CentralLabConfig",
    "dashboards",
    "reports",
    "system_workflows",
    "monitoring",
]


# =========================================================
# MIDDLEWARE
# =========================================================
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.PersistentSessionMiddleware",
    "core.middleware.SuperuserAuthorizationContextMiddleware",
    "core.middleware.AnonymousForbiddenRedirectMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
]


ROOT_URLCONF = "qad.urls"
WSGI_APPLICATION = "qad.wsgi.application"
ASGI_APPLICATION = "qad.asgi.application"


# =========================================================
# TEMPLATES
# =========================================================
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "admin_templates", BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.template.context_processors.static",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "CompanyInfo.context_processors.company_context",
                "accounts.context_processors.navigation_context",
                "qc.context_processors.treatment_plant_context",
                "weeklytasks.context_processors.water_treatment_chemical_analysis_context",
                "core.context_processors.sidebar_context",
                "sc.context_processors.shift_context",
            ],
        },
    },
]


# =========================================================
# DATABASE
# =========================================================
DATABASE_URL = env("DATABASE_URL")

if DATABASE_URL:
    u = urlparse(DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": u.path.lstrip("/"),
            "USER": u.username,
            "PASSWORD": u.password,
            "HOST": u.hostname,
            "PORT": u.port or 5432,
            "CONN_MAX_AGE": env("DB_CONN_MAX_AGE", 300, int),
            "ATOMIC_REQUESTS": True,
        }
    }
else:
    if ENVIRONMENT.strip().lower() in {"production", "prod"}:
        raise RuntimeError("DATABASE_URL is required in production; SQLite is development-only")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# =========================================================
# AUTH
# =========================================================
AUTH_USER_MODEL = "accounts.CustomUser"

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

LOGIN_URL = "accounts:login"
LOGOUT_REDIRECT_URL = "accounts:login"
SUPERUSER_LANDING_MAP = "home"

LOGIN_GROUP_REDIRECT_MAP = {
    "ADMIN": "accounts:dashboard",
    "STORES ASSISTANT":"ca:dashboard",
    "STORES SUPERVISOR": "accounts:dashboard",
    "PROCUREMENT OFFICER": "laboratory:dashboard",
    "LABORATORY TECHNICIAN": "qc_analytics:dashboard",
    "LABORATORY ASSISTANT": "qc_analytics:dashboard",
    "CHEMIST OFFICER": "qc:dashboard",
    "QUALITY ASSURANCE OFFICER": "qc:dashboard",
    "TREATMENT WORKS SUPERVISOR": "qc:dashboard",
    "TREATMENT WORKS OPERATOR": "qc:dashboard",
    "TREATMENT WORKS OFFICER": "qc:dashboard",
    "TREATMENT WORKS CO-ORDINATOR": "qc:dashboard",
    "QUALITY ASSURANCE  CO-ORDINATOR": "qc:dashboard",
    "WSP CO-ORDINATOR": "qc:dashboard",
    "QUALITY ASSURANCE MANAGER": "qc:dashboard",
    "MANAGING DIRECTOR": "qc:dashboard",
    "MAINTENANCE ASSISTANT": "qc:dashboard",
    "ARTISAN": "qc:dashboard",
    "MAINTENANCE SUPERVISOR": "qc:dashboard",
    "MAINTENANCE OFFICER": "qc:dashboard",
}


# =========================================================
# PASSWORDS
# =========================================================
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# =========================================================
# I18N
# =========================================================
LANGUAGE_CODE = "en-us"
USE_I18N = True
USE_TZ = True


# =========================================================
# STATIC / MEDIA
# =========================================================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
if DEBUG:
    STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
else:
    STATICFILES_STORAGE = "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


def ensure_runtime_dirs():
    for p in [MEDIA_ROOT, MEDIA_ROOT / "exports", MEDIA_ROOT / "uploads", BASE_DIR / "logs"]:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


ensure_runtime_dirs()


# =========================================================
# EMAIL
# =========================================================
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST")
EMAIL_PORT = env("EMAIL_PORT", 587, int)
EMAIL_USE_TLS = env("EMAIL_USE_TLS", True, bool)
EMAIL_HOST_USER = env("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER)


# =========================================================
# AXES (SECURITY)
# =========================================================
AXES_FAILURE_LIMIT = env("AXES_FAILURE_LIMIT", 10, int)
AXES_COOLOFF_TIME = env("AXES_COOLOFF_TIME", 1, int)

# =========================================================
# REDIS
# =========================================================

REDIS_URL = env(
    "REDIS_URL",
    "redis://127.0.0.1:6379/0",
)

REDIS_CACHE_URL = env(
    "REDIS_CACHE_URL",
    REDIS_URL,
)

REDIS_CHANNEL_URL = env(
    "REDIS_CHANNEL_URL",
    REDIS_URL,
)
# =========================================================
# CELERY
# =========================================================

CELERY_BROKER_URL = env(
    "CELERY_BROKER_URL",
    REDIS_URL,
)

CELERY_RESULT_BACKEND = env(
    "CELERY_RESULT_BACKEND",
    REDIS_URL,
)

CELERY_ACCEPT_CONTENT = ["json"]

CELERY_TASK_SERIALIZER = "json"

CELERY_RESULT_SERIALIZER = "json"

CELERY_TIMEZONE = TIME_ZONE

CELERY_ENABLE_UTC = True

CELERY_TASK_TRACK_STARTED = True

CELERY_TASK_ACKS_LATE = True

CELERY_TASK_REJECT_ON_WORKER_LOST = True

CELERY_TASK_IGNORE_RESULT = False

CELERY_TASK_ALWAYS_EAGER = False

CELERY_RESULT_EXPIRES = env(
    "CELERY_RESULT_EXPIRES",
    3600,
    int,
)

CELERY_WORKER_PREFETCH_MULTIPLIER = env(
    "CELERY_WORKER_PREFETCH_MULTIPLIER",
    1,
    int,
)

CELERY_WORKER_MAX_TASKS_PER_CHILD = env(
    "CELERY_WORKER_MAX_TASKS_PER_CHILD",
    1000,
    int,
)

CELERY_WORKER_CONCURRENCY = env(
    "CELERY_WORKER_CONCURRENCY",
    os.cpu_count() or 2,
    int,
)

CELERY_WORKER_DISABLE_RATE_LIMITS = False

CELERY_WORKER_SEND_TASK_EVENTS = True

CELERY_TASK_SEND_SENT_EVENT = True

CELERY_BROKER_CONNECTION_RETRY = True

CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

CELERY_BROKER_CONNECTION_MAX_RETRIES = None

CELERY_BROKER_HEARTBEAT = env(
    "CELERY_BROKER_HEARTBEAT",
    30,
    int,
)

CELERY_BROKER_POOL_LIMIT = env(
    "CELERY_BROKER_POOL_LIMIT",
    20,
    int,
)

CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": 3600,
}

CELERY_TASK_SOFT_TIME_LIMIT = env(
    "CELERY_TASK_SOFT_TIME_LIMIT",
    1500,
    int,
)

CELERY_TASK_TIME_LIMIT = env(
    "CELERY_TASK_TIME_LIMIT",
    1800,
    int,
)

CELERY_WORKER_MAX_TASKS_PER_CHILD = env(
    "CELERY_WORKER_MAX_TASKS_PER_CHILD",
    1000,
    int,
)

CELERY_WORKER_MAX_MEMORY_PER_CHILD = env(
    "CELERY_WORKER_MAX_MEMORY_PER_CHILD",
    300000,  # 300 MB max before worker child recycling
    int,
)

CELERY_BEAT_MAX_LOOP_INTERVAL = env(
    "CELERY_BEAT_MAX_LOOP_INTERVAL",
    60,
    int,
)

CELERY_BEAT_SCHEDULER = (
    "django_celery_beat.schedulers:DatabaseScheduler"
)

CELERY_BEAT_SCHEDULE = {
    "alerts.deliver_due_notifications": {
        "task": "alerts.tasks.deliver_due_alert_notifications",
        "schedule": 30.0,
    },
    "alerts.process_due_escalations": {
        "task": "alerts.tasks.process_due_alert_escalations",
        "schedule": 30.0,
    },
    "alerts.process_due_critical_repeats": {
        "task": "alerts.tasks.process_due_alert_repeats",
        "schedule": 30.0,
    },
    "qc.generate_due_analyses": {
        "task": "qc.generate_due_analyses",
        "schedule": 60.0,
    },
    "destw.refresh_analysis_workflow": {
        "task": "destw.refresh_analysis_workflow",
        "schedule": 60.0,
    },
    "kstw.refresh_analysis_workflow": {
        "task": "kstw.refresh_analysis_workflow",
        "schedule": 60.0,
    },
    "heavy_metal.refresh_analysis_workflow": {
        "task": "heavy_metal.refresh_analysis_workflow",
        "schedule": 60.0,
    },
    "microbial.refresh_analysis_workflow": {
        "task": "microbial.refresh_analysis_workflow",
        "schedule": 60.0,
    },
    "central_lab.refresh_analysis_workflow": {
        "task": "central_lab.refresh_analysis_workflow",
        "schedule": 60.0,
    },
    "qc.publish_qc_outbox_events": {
        "task": "qc.publish_qc_outbox_events",
        "schedule": 5.0,
    },
    "qc.prune_qc_outbox_events": {
        "task": "qc.prune_qc_outbox_events",
        "schedule": 86400.0,
    },
    "ca.auto_assign_due_chemical_analysts": {
        "task": "ca.auto_assign_due_chemical_analysts",
        "schedule": 60.0,
    },
    "ca.escalate_due_chemical_workflows": {
        "task": "ca.escalate_due_chemical_workflows",
        "schedule": 300.0,
    },
    "ca.publish_ca_outbox_events": {
        "task": "ca.publish_ca_outbox_events",
        "schedule": 15.0,
    },
}
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": env(
            "CHANNEL_BACKEND",
            "channels_redis.core.RedisChannelLayer",
        ),
        "CONFIG": {
            "hosts": [REDIS_CHANNEL_URL],
        },
    },
}


CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_CACHE_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": True,
        },
    },
}
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,

    "formatters": {
        "standard": {
            "format": (
                "%(asctime)s "
                "[%(levelname)s] "
                "%(name)s "
                "%(message)s"
            ),
        },
    },

    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },

    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },

    "loggers": {
        "celery": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },

        "qad": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# Use cached_db: fast reads from Redis, persistent writes to DB so
# sessions survive Redis eviction / container restarts.
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_CACHE_ALIAS = "default"

# Keep session alive across browser restarts (no more logout on tab close).
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# Long-lived session. Active requests refresh the expiry; there is no idle or
# daily-login logout policy. Users can still end the current session explicitly.
SESSION_COOKIE_AGE = 10 * 365 * 24 * 60 * 60
SESSION_SAVE_EVERY_REQUEST = True


# =========================================================
# SECURITY (ENV CONTROLLED)
# =========================================================
SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT", False, bool)
USE_X_FORWARDED_HOST = env("USE_X_FORWARDED_HOST", True, bool)

CSRF_COOKIE_SECURE = env("CSRF_COOKIE_SECURE", False, bool)
SESSION_COOKIE_SECURE = env("SESSION_COOKIE_SECURE", False, bool)

CSRF_COOKIE_HTTPONLY = env("CSRF_COOKIE_HTTPONLY", False, bool)
SESSION_COOKIE_HTTPONLY = env("SESSION_COOKIE_HTTPONLY", True, bool)

CSRF_COOKIE_SAMESITE = env("CSRF_COOKIE_SAMESITE", "Lax")
SESSION_COOKIE_SAMESITE = env("SESSION_COOKIE_SAMESITE", "Lax")

if ENVIRONMENT.strip().lower() in {"production", "prod"}:
    if "*" in ALLOWED_HOSTS:
        raise RuntimeError("ALLOWED_HOSTS must list explicit production hostnames")
    if not CSRF_COOKIE_SECURE or not SESSION_COOKIE_SECURE:
        raise RuntimeError("Secure session and CSRF cookies are required in production")


# =========================================================
# CSRF TRUSTED ORIGINS
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS", "").split(",")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

PROMETHEUS_ENABLED = env(
    "PROMETHEUS_ENABLED",
    False,
    bool,
)

PROMETHEUS_PORT = env(
    "PROMETHEUS_PORT",
    9100,
    int,
)
DATA_UPLOAD_MAX_NUMBER_FIELDS = env("DATA_UPLOAD_MAX_NUMBER_FIELDS",    20000,
    int,
)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "qad-locmem-cache",
        "TIMEOUT": 300,
    }
}
