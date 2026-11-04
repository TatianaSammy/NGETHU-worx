FROM python:3.11-slim-bookworm AS builder

LABEL stage="builder"

WORKDIR /build

# =============================================================================
# BUILD ARGUMENTS
# =============================================================================

ARG BUILD_DATE
ARG VERSION=latest
ARG VCS_REF=unknown

# =============================================================================
# ENVIRONMENT
# =============================================================================

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Africa/Nairobi \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONOPTIMIZE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=25 \
    PIP_PROGRESS_BAR=off \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_NO_CACHE_DIR=1

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# =============================================================================
# APT HARDENING
# =============================================================================

RUN set -eux; \
    echo 'Acquire::Retries "10";' > /etc/apt/apt.conf.d/99custom; \
    echo 'Acquire::http::Timeout "120";' >> /etc/apt/apt.conf.d/99custom; \
    echo 'Acquire::https::Timeout "120";' >> /etc/apt/apt.conf.d/99custom; \
    echo 'Acquire::ForceIPv4 "true";' >> /etc/apt/apt.conf.d/99custom

# =============================================================================
# BUILD DEPENDENCIES
# =============================================================================

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        make \
        pkg-config \
        cmake \
        ninja-build \
        meson \
        git \
        curl \
        wget \
        cargo \
        rustc \
        python3-dev \
        gfortran \
        libpq-dev \
        libffi-dev \
        libssl-dev \
        zlib1g-dev \
        libjpeg-dev \
        libcairo2-dev \
        libpango1.0-dev \
        libgdk-pixbuf-2.0-dev \
        libxml2-dev \
        libxslt1-dev \
        libfreetype6-dev \
        libopenjp2-7-dev \
        libtiff-dev \
        libwebp-dev \
        libharfbuzz-dev \
        libfribidi-dev \
        libblas-dev \
        liblapack-dev \
        libatlas-base-dev \
        libyaml-dev \
        libxmlsec1-dev \
        libxmlsec1-openssl \
        libmagic-dev \
        shared-mime-info \
        ca-certificates \
        tzdata \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# =============================================================================
# PYTHON TOOLCHAIN
# =============================================================================

RUN python -m pip install --upgrade \
        pip \
        setuptools \
        wheel \
        build \
        cython \
        meson-python

# =============================================================================
# REQUIREMENTS
# =============================================================================

COPY requirements.txt .

# =============================================================================
# BUILD WHEELS
# =============================================================================

RUN set -eux; \
    mkdir -p /wheels; \
    pip wheel \
        --wheel-dir=/wheels \
        --prefer-binary \
        --timeout=300 \
        --retries=25 \
        -r requirements.txt

# =============================================================================
# RUNTIME STAGE
# =============================================================================

FROM python:3.11-slim-bookworm AS runtime

WORKDIR /app

# =============================================================================
# BUILD ARGUMENTS
# =============================================================================

ARG BUILD_DATE
ARG VERSION=latest
ARG VCS_REF=unknown

ARG UID=10001
ARG GID=10001

# =============================================================================
# OCI LABELS
# =============================================================================

LABEL maintainer="QAD Platform" \
      org.opencontainers.image.title="QAD Backend" \
      org.opencontainers.image.description="QAD Enterprise Django Runtime" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="QAD"

# =============================================================================
# ENVIRONMENT
# =============================================================================

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Africa/Nairobi \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONOPTIMIZE=1 \
    PYTHONPATH=/app \
    DJANGO_SETTINGS_MODULE=qad.settings \
    ENVIRONMENT=production \
    DEBUG=False \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/home/appuser/.local/bin:${PATH}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# =============================================================================
# RUNTIME PACKAGES
# =============================================================================

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        tini \
        dumb-init \
        bash \
        curl \
        wget \
        ca-certificates \
        tzdata \
        procps \
        iputils-ping \
        dnsutils \
        netcat-openbsd \
        redis-tools \
        postgresql-client \
        gosu \
        jq \
        vim-tiny \
        libpq5 \
        libcairo2 \
        libpango-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libxml2 \
        libxslt1.1 \
        libjpeg62-turbo \
        libfreetype6 \
        libopenjp2-7 \
        libtiff6 \
        libwebp7 \
        libharfbuzz0b \
        libfribidi0 \
        libblas3 \
        liblapack3 \
        libmagic1 \
        espeak-ng \
        shared-mime-info \
    && apt-get purge -y --auto-remove \
    && apt-get clean \
    && rm -rf \
        /var/lib/apt/lists/* \
        /tmp/* \
        /var/tmp/*

# =============================================================================
# TIMEZONE
# =============================================================================

RUN ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo "${TZ}" > /etc/timezone

# =============================================================================
# NON-ROOT USER
# =============================================================================

RUN set -eux; \
    groupadd --system --gid ${GID} appuser; \
    useradd \
        --system \
        --uid ${UID} \
        --gid ${GID} \
        --create-home \
        --home-dir /home/appuser \
        --shell /usr/sbin/nologin \
        appuser

# =============================================================================
# APPLICATION DIRECTORIES
# =============================================================================

RUN set -eux; \
    mkdir -p \
        /app/staticfiles \
        /app/media \
        /app/media/exports \
        /app/logs \
        /app/run \
        /app/tmp \
        /var/log/qad \
        /var/run/qad \
        /home/appuser/.cache \
    && chown -R appuser:appuser \
        /app \
        /var/log/qad \
        /var/run/qad \
        /home/appuser

# =============================================================================
# PYTHON DEPENDENCIES
# =============================================================================

COPY --from=builder /wheels /wheels
COPY requirements.txt .

RUN set -eux; \
    pip install --no-cache-dir /wheels/*; \
    rm -rf /wheels

# =============================================================================
# APPLICATION SOURCE
# =============================================================================

COPY --chown=appuser:appuser . /app

# =============================================================================
# ENTRYPOINT
# =============================================================================

COPY --chown=appuser:appuser wait-for-services.sh /usr/local/bin/entrypoint.sh

RUN chmod 750 /usr/local/bin/entrypoint.sh

# =============================================================================
# PYTHON BYTECODE
# =============================================================================

RUN python -m compileall -q /app || true

# =============================================================================
# FILESYSTEM HARDENING
# =============================================================================

RUN set -eux; \
    find /app -type d -exec chmod 750 {} \;; \
    find /app -type f -exec chmod 640 {} \;; \
    find /app -type f -name "*.sh" -exec chmod 750 {} \;; \
    chmod 770 \
        /app/logs \
        /app/run \
        /app/tmp \
        /app/media \
        /app/media/exports \
        /app/staticfiles; \
    chown -R appuser:appuser /app

# =============================================================================
# REMOVE SENSITIVE ARTIFACTS
# =============================================================================

RUN set -eux; \
    find /usr/local/lib/python3.11 \
        -type d \
        -name "tests" \
        -exec rm -rf {} + || true

# =============================================================================
# NETWORK
# =============================================================================

EXPOSE 8000

STOPSIGNAL SIGTERM

# =============================================================================
# HEALTHCHECK
# =============================================================================

HEALTHCHECK --interval=20s \
            --timeout=10s \
            --start-period=240s \
            --retries=10 \
CMD curl --fail --silent --max-time 5 http://127.0.0.1:8000/health/ || exit 1

# =============================================================================
# SECURITY
# =============================================================================

USER appuser

# =============================================================================
# ENTRYPOINT
# =============================================================================

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]

# =============================================================================
# DEFAULT CMD
# =============================================================================

CMD ["gunicorn", \
     "qad.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--threads", "2", \
     "--worker-class", "gthread", \
     "--worker-tmp-dir", "/dev/shm", \
     "--max-requests", "5000", \
     "--max-requests-jitter", "500", \
     "--timeout", "180", \
     "--graceful-timeout", "30", \
     "--keep-alive", "10", \
     "--backlog", "4096", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--capture-output", \
     "--log-level", "info"]
