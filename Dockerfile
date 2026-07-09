# Stage 1: Install production dependencies
FROM public.ecr.aws/docker/library/python:3.12-alpine3.20 AS python_packages

ENV COLUMNS=300

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /uvx /usr/local/bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Define build args
ARG RUN_UV_LOCK=false

# Install common tools (git is required for some Python packages)
RUN apk add --no-cache git

WORKDIR /usr/src/patient-matching-service

# Copy dependency files
COPY pyproject.toml uv.lock* /usr/src/patient-matching-service/

# Conditionally re-lock if requested
RUN if [ "$RUN_UV_LOCK" = "true" ]; then echo "Locking dependencies" && rm -f uv.lock && uv lock --verbose; fi

# Install production dependencies
RUN uv sync --frozen --all-extras --no-install-project --verbose

# Copy lock file to /tmp for retrieval
RUN cp -f uv.lock /tmp/uv.lock

# Stage 1b: Install dev dependencies (extends production)
FROM python_packages AS python_packages_dev

RUN uv sync --frozen --all-extras --group dev --no-install-project --verbose

# Stage 2: Production runtime
FROM public.ecr.aws/docker/library/python:3.12-alpine3.20 AS production

ENV COLUMNS=300
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PROJECT_DIR=/usr/src/patient-matching-service
ENV PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus

# Install runtime dependencies
RUN apk add --no-cache curl libstdc++ libffi git

# Create Prometheus metrics directory
RUN mkdir -p ${PROMETHEUS_MULTIPROC_DIR}

WORKDIR ${PROJECT_DIR}

# Copy installed packages from build stage
COPY --from=python_packages /opt/venv /opt/venv

# Copy dependency files
COPY pyproject.toml ${PROJECT_DIR}/

# Copy uv.lock from build stage (in case it was re-locked)
COPY --from=python_packages /tmp/uv.lock ${PROJECT_DIR}/uv.lock
COPY --from=python_packages /tmp/uv.lock /tmp/uv.lock

# Copy application code
COPY ./patientmatchingservice ${PROJECT_DIR}/patientmatchingservice

# Expose port 5000
EXPOSE 5000

# Create restricted user
USER root
RUN addgroup -S appgroup && adduser -S -h /etc/appuser appuser -G appgroup
RUN chown -R appuser:appgroup ${PROJECT_DIR} /opt/venv ${PROMETHEUS_MULTIPROC_DIR}
USER appuser

CMD ["opentelemetry-instrument", "uvicorn", "patientmatchingservice.api:app", "--host", "0.0.0.0", "--port", "5000", "--workers", "4", "--log-level", "debug"]

# Stage 3: Development runtime (extends production with dev deps and tests)
FROM production AS development

USER root
COPY --from=python_packages_dev /opt/venv /opt/venv
COPY ./tests ${PROJECT_DIR}/tests
RUN chown -R appuser:appgroup /opt/venv ${PROJECT_DIR}/tests
USER appuser

CMD ["uvicorn", "patientmatchingservice.api:app", "--host", "0.0.0.0", "--port", "5000", "--reload", "--log-level", "debug"]

# Default target is production
FROM production
