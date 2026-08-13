# Stage 1: Production dependencies
# This stage installs production Python dependencies using uv
FROM 856965016623.dkr.ecr.us-east-1.amazonaws.com/root-mirror/python:3.12-alpine3.22 AS python_packages

# Set terminal width (COLUMNS) and height (LINES)
ENV COLUMNS=300

# Setup JFrog Auth (APPEND to .netrc — preserves existing entries)
RUN --mount=type=secret,id=jfrog_read_user --mount=type=secret,id=jfrog_read_token \
    JFROG_READ_USER=$(cat /run/secrets/jfrog_read_user) && \
    JFROG_READ_TOKEN=$(cat /run/secrets/jfrog_read_token) && \
    echo "machine artifacts.bwell.com login $JFROG_READ_USER password $JFROG_READ_TOKEN" >> ~/.netrc && \
    chmod 600 ~/.netrc

# Install secure-apk, rootio-patcher, uv, and git from JFrog Alpine repos (not ghcr.io/
# dl-cdn.alpinelinux.org). uv must come from apk here, not `COPY --from=ghcr.io/astral-sh/uv`
# (a public registry pull) -- this block runs BEFORE `uv sync` below for that reason.
# apk authenticates via the ~/.netrc written above (Alpine 3.22+), so repo URLs stay clean
# (no inline creds, no sed-strip needed). Key downloads keep inline creds because busybox
# wget does not read .netrc. rootio-alpine listed first so apk prefers Root.io-patched
# OS packages.
RUN --mount=type=secret,id=jfrog_read_user --mount=type=secret,id=jfrog_read_token \
    ALPINE_MINOR=$(cat /etc/alpine-release | cut -d. -f1,2) && \
    CREDS="$(cat /run/secrets/jfrog_read_user):$(cat /run/secrets/jfrog_read_token)" && \
    wget -qO /etc/apk/keys/alpine.rsa.pub \
        "https://${CREDS}@artifacts.bwell.com/artifactory/api/security/keypair/public/repositories/private-alpine" && \
    wget -qO "/etc/apk/keys/root@alpinelinux.org.rsa.pub" \
        "https://${CREDS}@artifacts.bwell.com/artifactory/vendor-public-keys/rootio-alpine.pub" && \
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

# Show the current pip configuration (for debugging purposes)
RUN pip config list

# Install all production dependencies using uv (JFrog index auth via BuildKit secret,
# env-var auth NOT .netrc -- uv reads UV_INDEX_JFROG_* per the index name in pyproject.toml)
RUN --mount=type=secret,id=jfrog_read_token \
    set -eu; \
    export UV_INDEX_JFROG_USERNAME=""; \
    export UV_INDEX_JFROG_PASSWORD="$(cat /run/secrets/jfrog_read_token)"; \
    uv sync --frozen --all-extras --no-install-project --verbose

# Validate dependencies against Root.io vulnerability database (dry-run only). uv venvs
# omit pip, which rootio_patcher needs to inventory packages -- bootstrap it from the
# interpreter's bundled wheels first (no network/auth needed), then upgrade pip through
# the JFrog index. pip is only here to satisfy the inventory, not copied to the runtime image.
RUN /opt/venv/bin/python -m ensurepip >/dev/null && \
    PIP_INDEX_URL=https://artifacts.bwell.com/artifactory/api/pypi/virtual-pypi/simple /opt/venv/bin/python -m pip install --upgrade pip && \
    ROOTIO_PKG_URL=https://artifacts.bwell.com/artifactory/api \
    ROOTIO_PIP_INDEX_URL=https://artifacts.bwell.com/artifactory/api/pypi/virtual-pypi/simple \
    rootio_patcher pip remediate --dry-run --python-path=/opt/venv/bin/python

# Remove credentials
RUN rm -rf ~/.netrc

# Copy uv.lock from working directory to /tmp for retrieval if needed
RUN cp -n /usr/src/patient_matching_service/uv.lock /tmp/uv.lock

# Create necessary directories and list their contents (for debugging and verification)
RUN mkdir -p /opt/venv/lib/python3.12/site-packages && ls -halt /opt/venv/lib/python3.12/site-packages
RUN mkdir -p /opt/venv/bin && ls -halt /opt/venv/bin

# Check and print system and Python platform information (for debugging)
RUN python -c "import platform; print(platform.platform()); print(platform.architecture())"
RUN python -c "import sys; print(sys.platform, sys.version, sys.maxsize > 2**32)"

# Debug pip installation and list installed packages with verbosity
RUN pip debug --verbose
RUN pip list -v

# Stage 1b: Development dependencies (extends production)
# This stage installs dev dependencies on top of production
FROM python_packages AS python_packages_dev

RUN --mount=type=secret,id=jfrog_read_token \
    set -eu; \
    export UV_INDEX_JFROG_USERNAME=""; \
    export UV_INDEX_JFROG_PASSWORD="$(cat /run/secrets/jfrog_read_token)"; \
    uv sync --frozen --all-extras --group dev --no-install-project --verbose

# Stage 2: Production runtime image
FROM 856965016623.dkr.ecr.us-east-1.amazonaws.com/root-mirror/python:3.12-alpine3.22 AS production

# Set terminal width (COLUMNS) and height (LINES)
ENV COLUMNS=300

# Configure JFrog Alpine repos and install runtime OS deps from the hardened mirror
# instead of the public Alpine CDN. This is a fresh stage (not FROM=builder) so it needs
# its own .netrc for apk auth -- same as the builder stage above.
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

# Copy Pipfile.lock to a temporary directory so it can be retrieved if needed
COPY --from=python_packages /tmp/uv.lock /tmp/uv.lock

# Create directories and list their contents (for debugging and verification)
RUN mkdir -p /opt/venv/lib/python3.12/site-packages && ls -halt /opt/venv/lib/python3.12/site-packages
RUN mkdir -p /opt/venv/bin && ls -halt /opt/venv/bin

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
