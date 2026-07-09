# Stage 1: Production dependencies
# This stage installs production Python dependencies using uv
FROM public.ecr.aws/docker/library/python:3.12-alpine3.20 AS python_packages

# Set terminal width (COLUMNS) and height (LINES)
ENV COLUMNS=300

# Copy uv binary from official uv image
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /usr/local/bin/

# Set environment variables for uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install common tools and dependencies (git is required for some Python packages)
RUN apk add --no-cache git

# Set the working directory inside the container
WORKDIR /usr/src/patient_matching_service

# Copy pyproject.toml and uv.lock to the working directory
COPY pyproject.toml uv.lock* /usr/src/patient_matching_service/

# Show the current pip configuration (for debugging purposes)
RUN pip config list

# Install all production dependencies using uv
RUN uv sync --frozen --all-extras --no-install-project --verbose

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

RUN uv sync --frozen --all-extras --group dev --no-install-project --verbose

# Stage 2: Production runtime image
FROM public.ecr.aws/docker/library/python:3.12-alpine3.20 AS production

# Set terminal width (COLUMNS) and height (LINES)
ENV COLUMNS=300

# Install runtime dependencies required by the application
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
