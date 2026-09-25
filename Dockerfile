# Stage 1: Production dependencies
# This stage installs production Python dependencies using uv
FROM 856965016623.dkr.ecr.us-east-1.amazonaws.com/root-mirror/python:3.12-alpine3.22 AS python_packages

# Set terminal width (COLUMNS) and height (LINES)
ENV COLUMNS=300

# Configure JFrog Alpine repos, then install secure-apk, rootio-patcher, and uv from apk
# (no public registry pulls -- uv no longer comes from ghcr.io/astral-sh/uv) plus git,
# which this project already needed. uv must come from apk so the whole toolchain is
# sourced from the hardened JFrog/Root.io mirrors, which means this repo setup has to run
# BEFORE `uv sync` (the opposite order from the old ghcr.io-based copy).
#
# Auth: JFrog creds go to /root/.netrc so apk can authenticate; repo URLs stay clean (no
# inline creds). This is the builder stage, which is discarded (only /opt/venv is copied
# into later stages), so the .netrc left behind here never reaches a shipped image.
# Requires Alpine v3.22+ for apk's .netrc support -- this image is alpine3.22, so OK.
RUN --mount=type=secret,id=jfrog_read_user --mount=type=secret,id=jfrog_read_token \
    ALPINE_MINOR=$(cat /etc/alpine-release | cut -d. -f1,2) && \
    JF_USER="$(cat /run/secrets/jfrog_read_user)" && \
    JF_TOKEN="$(cat /run/secrets/jfrog_read_token)" && \
    CREDS="${JF_USER}:${JF_TOKEN}" && \
    wget -qO /etc/apk/keys/alpine.rsa.pub \
        "https://${CREDS}@artifacts.bwell.com/artifactory/api/security/keypair/public/repositories/private-alpine" && \
    wget -qO "/etc/apk/keys/root@alpinelinux.org.rsa.pub" \
        "https://${CREDS}@artifacts.bwell.com/artifactory/vendor-public-keys/rootio-alpine.pub" && \
    printf 'machine artifacts.bwell.com login %s password %s\n' "$JF_USER" "$JF_TOKEN" > /root/.netrc && \
    chmod 600 /root/.netrc && \
    echo "https://artifacts.bwell.com/artifactory/rootio-alpine/${ALPINE_MINOR}"            >  /etc/apk/repositories && \
    echo "https://artifacts.bwell.com/artifactory/global-alpine/v${ALPINE_MINOR}/main"      >> /etc/apk/repositories && \
    echo "https://artifacts.bwell.com/artifactory/global-alpine/v${ALPINE_MINOR}/community" >> /etc/apk/repositories && \
    echo "https://artifacts.bwell.com/artifactory/private-alpine/main/${ALPINE_MINOR}"      >> /etc/apk/repositories && \
    apk update && \
    apk add --no-cache secure-apk rootio-patcher uv git

# Set environment variables for uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Set the working directory inside the container
WORKDIR /usr/src/patient_matching_service

# Copy pyproject.toml and uv.lock to the working directory
COPY pyproject.toml uv.lock* /usr/src/patient_matching_service/

# Install all production dependencies using uv. Auth via JFrog index env vars -- uv reads
# UV_INDEX_JFROG_USERNAME/PASSWORD ("jfrog" is the index name in pyproject.toml), passed as
# BuildKit secrets, never written to disk. Uses the real JFROG_READ_USER, not an empty
# string -- Artifactory's virtual-pypi rejects an empty username with 403 even given a
# valid token as the password (confirmed directly against this same index elsewhere).
RUN --mount=type=secret,id=jfrog_read_user --mount=type=secret,id=jfrog_read_token \
    set -eu; \
    export UV_INDEX_JFROG_USERNAME="$(cat /run/secrets/jfrog_read_user)"; \
    export UV_INDEX_JFROG_PASSWORD="$(cat /run/secrets/jfrog_read_token)"; \
    uv sync --frozen --all-extras --no-install-project --verbose

# Validate dependencies against the Root.io vulnerability database (dry-run only).
# rootio_patcher inspects the venv via `python -m pip list`, but uv-created venvs omit
# pip -- install it through the JFrog index so the inventory is accurate. pip is only
# here to satisfy that inventory and is never copied to a runtime image (only /opt/venv
# is copied forward).
#
# Installed with `uv pip install`, NOT `python -m ensurepip`: this base image patches
# ensurepip._PACKAGE_NAMES to ('rootio_setuptools', 'rootio_pip') but bundles no
# rootio_setuptools wheel in ensurepip/_bundled/, and the bootstrap installs --no-index
# --find-links against that same directory -- so the call fails outright ("No matching
# distribution found for rootio_setuptools") and breaks every build in this repo
# regardless of diff. It fails at this step, so it reads as a dependency problem when
# the build actually died before the patcher ran. uv needs no pre-existing pip, so this
# sidesteps the patched module entirely. Unpinned, matching the previous
# `--upgrade pip` behaviour (root.io patches specific older pins, not head).
#
# Auth via the same UV_INDEX_JFROG_USERNAME/PASSWORD + named `--index jfrog=...` used
# by the `uv sync` call above (name comes from pyproject.toml's [[tool.uv.index]]),
# not `https://:$TOKEN@...` interpolated into --index-url: that form puts the token in
# uv's process arguments, which Aikido flagged on the identical line in
# bwell-ai-plugin-marketplace#223.
RUN --mount=type=secret,id=jfrog_read_user --mount=type=secret,id=jfrog_read_token \
    set -eu; \
    export UV_INDEX_JFROG_USERNAME="$(cat /run/secrets/jfrog_read_user)"; \
    export UV_INDEX_JFROG_PASSWORD="$(cat /run/secrets/jfrog_read_token)"; \
    uv pip install --python /opt/venv/bin/python --no-cache \
    --index jfrog=https://artifacts.bwell.com/artifactory/api/pypi/virtual-pypi/simple \
    pip && \
    ROOTIO_PKG_URL=https://artifacts.bwell.com/artifactory/api \
    ROOTIO_PIP_INDEX_URL=https://artifacts.bwell.com/artifactory/api/pypi/virtual-pypi/simple \
    rootio_patcher pip remediate --dry-run --python-path=/opt/venv/bin/python

# Remove JFrog credentials (this stage is discarded; only /opt/venv is copied forward)
RUN rm -rf ~/.netrc

# Copy uv.lock from working directory to /tmp for retrieval if needed
RUN cp -n /usr/src/patient_matching_service/uv.lock /tmp/uv.lock

# Stage 1b: Development dependencies (extends production)
# This stage installs dev dependencies on top of production
FROM python_packages AS python_packages_dev

RUN --mount=type=secret,id=jfrog_read_user --mount=type=secret,id=jfrog_read_token \
    set -eu; \
    export UV_INDEX_JFROG_USERNAME="$(cat /run/secrets/jfrog_read_user)"; \
    export UV_INDEX_JFROG_PASSWORD="$(cat /run/secrets/jfrog_read_token)"; \
    uv sync --frozen --all-extras --group dev --no-install-project --verbose

# Stage 2: Production runtime image
FROM 856965016623.dkr.ecr.us-east-1.amazonaws.com/root-mirror/python:3.12-alpine3.22 AS production

# Set terminal width (COLUMNS) and height (LINES)
ENV COLUMNS=300

# Configure JFrog Alpine repos (temporarily -- cleaned up at the end of this RUN so no
# credentials persist in this stage's layers, since this stage IS a shipped image, not a
# discarded builder stage) and install runtime OS deps from the hardened mirror instead of
# the public Alpine CDN. Same auth pattern as the builder stage; see the comment there for
# the Alpine-version requirement.
RUN --mount=type=secret,id=jfrog_read_user --mount=type=secret,id=jfrog_read_token \
    ALPINE_MINOR=$(cat /etc/alpine-release | cut -d. -f1,2) && \
    JF_USER="$(cat /run/secrets/jfrog_read_user)" && \
    JF_TOKEN="$(cat /run/secrets/jfrog_read_token)" && \
    CREDS="${JF_USER}:${JF_TOKEN}" && \
    wget -qO /etc/apk/keys/alpine.rsa.pub \
        "https://${CREDS}@artifacts.bwell.com/artifactory/api/security/keypair/public/repositories/private-alpine" && \
    wget -qO "/etc/apk/keys/root@alpinelinux.org.rsa.pub" \
        "https://${CREDS}@artifacts.bwell.com/artifactory/vendor-public-keys/rootio-alpine.pub" && \
    printf 'machine artifacts.bwell.com login %s password %s\n' "$JF_USER" "$JF_TOKEN" > /root/.netrc && \
    chmod 600 /root/.netrc && \
    echo "https://artifacts.bwell.com/artifactory/rootio-alpine/${ALPINE_MINOR}"            >  /etc/apk/repositories && \
    echo "https://artifacts.bwell.com/artifactory/global-alpine/v${ALPINE_MINOR}/main"      >> /etc/apk/repositories && \
    echo "https://artifacts.bwell.com/artifactory/global-alpine/v${ALPINE_MINOR}/community" >> /etc/apk/repositories && \
    echo "https://artifacts.bwell.com/artifactory/private-alpine/main/${ALPINE_MINOR}"      >> /etc/apk/repositories && \
    apk update && \
    apk add --no-cache curl libstdc++ libffi git && \
    rm -f /root/.netrc

# Set environment variables for project configuration
ENV PROJECT_DIR=/usr/src/patient_matching_service
ENV PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus
ENV PIP_ROOT_USER_ACTION=ignore
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create the directory for Prometheus metrics
RUN mkdir -p ${PROMETHEUS_MULTIPROC_DIR}

# Set the working directory for the project
WORKDIR ${PROJECT_DIR}

# Copy the application code into the runtime image (NO tests directory)
COPY ./patient_matching_service ${PROJECT_DIR}/patient_matching_service

# Copy installed Python packages from the previous stage
COPY --from=python_packages /opt/venv /opt/venv

# Copy uv.lock to a temporary directory so it can be retrieved if needed
COPY --from=python_packages /tmp/uv.lock /tmp/uv.lock

# Expose port 5000 for the application
EXPOSE 5000

# Switch to the root user to perform user management tasks
USER root

# Create a restricted user (appuser) and group (appgroup) for running the application
RUN addgroup -S appgroup && adduser -S -h /etc/appuser appuser -G appgroup

# Ensure that the appuser owns the application files and directories
RUN chown -R appuser:appgroup ${PROJECT_DIR} /opt/venv ${PROMETHEUS_MULTIPROC_DIR}

# Switch to the restricted user to enhance security
USER appuser

# PYTHONPATH is prepended with the OTel Operator's auto-instrumentation bundle
# in envs where it's enabled (otel.autoInstrumentation.enabled: true in
# dev-ue1/staging-ue1 Helm values, now in icanbwell/bwell-cms-hte-patient-matching-service), which shadows our own
# installed packages with its own frozen copies (e.g. typing_extensions) --
# see person-matching-service PR #154 / BAI-622 for the root-cause writeup.
# Re-prepending our venv here restores normal precedence: our packages
# resolve first, the bundle's are only a fallback for what we don't have
# (i.e. the auto-instrumentation loader itself). The path is asked from
# sysconfig rather than hardcoded (e.g. /opt/venv/lib/python3.12/site-packages)
# so a future base-image Python bump can't silently turn this into a no-op.
CMD ["sh", "-c", "\
    VENV_SITE_PACKAGES=$(python -c 'import sysconfig; print(sysconfig.get_path(\"purelib\"))') && \
    export PYTHONPATH=\"${VENV_SITE_PACKAGES}${PYTHONPATH:+:$PYTHONPATH}\" && \
    exec uvicorn patient_matching_service.api:app \
        --host 0.0.0.0 \
        --port 5000 \
        --workers \"${NUM_WORKERS:-1}\" \
        --timeout-graceful-shutdown 25 \
"]

# Stage 3: Development runtime (extends production with dev deps, tests, and hot reload)
FROM production AS development

USER root
# Copy dev dependencies (superset of production)
COPY --from=python_packages_dev /opt/venv /opt/venv
# Copy tests directory for development
COPY ./tests ${PROJECT_DIR}/tests
RUN chown -R appuser:appgroup /opt/venv ${PROJECT_DIR}/tests
USER appuser

# Override CMD with hot-reload for local development
CMD ["uvicorn", "patient_matching_service.api:app", "--host", "0.0.0.0", "--port", "5000", "--reload"]

# Default: bare `docker build .` produces production image
FROM production
