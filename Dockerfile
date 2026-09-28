# This repo is temporarily public (BAI-894) and carries no JFrog/AWS credentials as a
# result -- org-scoped secrets and self-hosted-runner access don't reach a public repo.
# Everything below resolves from public sources (public.ecr.aws, public Alpine CDN,
# public PyPI), matching icanbwell/kill-the-clipboard-scanner's own public Dockerfile.
# The hardened build (Root.io-mirror base, JFrog-proxied/rootio-patched OS packages, a
# prebuilt JFrog-hosted duckdb wheel) lives in bwell.Dockerfile in
# icanbwell/bwell-cms-hte-patient-matching-service, the repo that still has JFrog/ECR
# access.
#
# INSTALL_MATCHING_ENGINE controls whether cms-hte-patient-matching (the `matching` extra
# in pyproject.toml) gets installed. It pulls in duckdb, which publishes no musllinux
# (Alpine) wheel for the version this repo needs -- see icanbwell/python-alpine-wheels,
# which builds one, but only publishes it to JFrog. Without JFrog access, duckdb has to
# be compiled from source instead (needs a C++ toolchain, added below only when this arg
# is true) -- see patient_matching_service/service/match_controller.py's
# MATCHING_ENGINE_AVAILABLE guard for how the app degrades when it's false.
ARG INSTALL_MATCHING_ENGINE=false

# Stage 1: Production dependencies
# This stage installs production Python dependencies using uv
FROM public.ecr.aws/docker/library/python:3.12-alpine3.22 AS python_packages
ARG INSTALL_MATCHING_ENGINE

# Set terminal width (COLUMNS) and height (LINES)
ENV COLUMNS=300

# Install uv from its own public image (no public registry pulls needed beyond this).
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/

# git is required for some Python packages. build-base/cmake are only needed to compile
# duckdb from source when INSTALL_MATCHING_ENGINE=true (see the file-level comment above)
# -- skipped otherwise so the default build stays fast and minimal.
RUN if [ "$INSTALL_MATCHING_ENGINE" = "true" ]; then \
        apk add --no-cache git build-base cmake; \
    else \
        apk add --no-cache git; \
    fi

# Set environment variables for uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Set the working directory inside the container
WORKDIR /usr/src/patient_matching_service

# Copy pyproject.toml and uv.lock to the working directory
COPY pyproject.toml uv.lock* /usr/src/patient_matching_service/

# Install all production dependencies using uv (public PyPI -- no JFrog auth needed).
# --extra matching (cms-hte-patient-matching + duckdb, compiled from source above) only
# when explicitly requested.
RUN if [ "$INSTALL_MATCHING_ENGINE" = "true" ]; then \
        uv sync --frozen --extra matching --no-install-project --verbose; \
    else \
        uv sync --frozen --no-install-project --verbose; \
    fi

# Copy uv.lock from working directory to /tmp for retrieval if needed
RUN cp -n /usr/src/patient_matching_service/uv.lock /tmp/uv.lock

# Stage 1b: Development dependencies (extends production)
# This stage installs dev dependencies on top of production
FROM python_packages AS python_packages_dev
ARG INSTALL_MATCHING_ENGINE

RUN if [ "$INSTALL_MATCHING_ENGINE" = "true" ]; then \
        uv sync --frozen --extra matching --group dev --no-install-project --verbose; \
    else \
        uv sync --frozen --group dev --no-install-project --verbose; \
    fi

# Stage 2: Production runtime image
FROM public.ecr.aws/docker/library/python:3.12-alpine3.22 AS production

# Set terminal width (COLUMNS) and height (LINES)
ENV COLUMNS=300

# Install runtime OS deps from the public Alpine CDN.
RUN apk add --no-cache curl libstdc++ libffi git

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
