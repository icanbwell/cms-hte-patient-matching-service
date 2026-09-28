FROM public.ecr.aws/docker/library/python:3.12-alpine3.22 AS python_packages

ENV COLUMNS=300

# Install uv from its own public image, git/build-base from the public Alpine CDN -- no
# JFrog/Root.io involvement, matching the main Dockerfile (see its file-level comment).
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/
RUN apk add --no-cache git build-base

ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./

# No --extra matching: lint/type-check doesn't need the real cms-hte-patient-matching
# installed (see pyproject.toml's [[tool.mypy.overrides]] for patient_matching.*), and
# skipping it here avoids compiling duckdb from source just to run pre-commit.
RUN uv sync --frozen --group dev --no-install-project --verbose

ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /sourcecode

# --system (not --global) so this applies no matter which user ends up
# running pre-commit -- avoids needing a per-user git config for whichever
# UID the container ends up running as (see below).
RUN git config --system --add safe.directory /sourcecode

# pre-commit-hook mounts a named volume at /.cache/pre-commit for pre-commit's
# cache; PRE_COMMIT_HOME points pre-commit at it directly (its default,
# $HOME/.cache/pre-commit, would nest one level too deep here). Also set HOME
# itself as a general fallback for anything else that consults it. Both need
# to be world-writable since the actual runtime UID is unknown at build time
# (see below) -- an arbitrary numeric UID has no passwd entry, and thus no
# resolvable $HOME, unless we pin one.
ENV HOME=/.cache/pre-commit
ENV PRE_COMMIT_HOME=/.cache/pre-commit
RUN mkdir -p /.cache/pre-commit && chmod 777 /.cache/pre-commit

# Run pre-commit as an unprivileged user rather than root. /sourcecode is
# bind-mounted at container start (not baked into the image), owned by
# whichever host/CI user checked it out -- pre-commit-hook runs this image
# with `--user "$(id -u):$(id -g)"` so the container process matches that
# ownership exactly, rather than chowning the mount (fragile: chown -R over
# a live git checkout hits read-only .git/objects entries and fails). This
# USER is only the default for when the image is run without --user.
RUN addgroup -S appgroup && adduser -S -h /etc/appuser appuser -G appgroup
USER appuser

CMD ["pre-commit", "run", "--all-files"]
