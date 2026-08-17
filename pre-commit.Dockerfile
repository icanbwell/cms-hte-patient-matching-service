FROM 856965016623.dkr.ecr.us-east-1.amazonaws.com/root-mirror/python:3.12-alpine3.22 AS python_packages

ENV COLUMNS=300

# Configure JFrog Alpine repos, then install secure-apk, rootio-patcher, and uv from apk
# (no public registry pulls -- uv no longer comes from ghcr.io/astral-sh/uv) plus
# git/build-base, which this lint-only image already needed. See the main Dockerfile for
# the full explanation of this pattern (same auth model, same Alpine-version requirement).
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
    apk add --no-cache secure-apk rootio-patcher uv git build-base && \
    rm -f /root/.netrc

ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./

RUN --mount=type=secret,id=jfrog_read_user --mount=type=secret,id=jfrog_read_token \
    set -eu; \
    export UV_INDEX_JFROG_USERNAME="$(cat /run/secrets/jfrog_read_user)"; \
    export UV_INDEX_JFROG_PASSWORD="$(cat /run/secrets/jfrog_read_token)"; \
    uv sync --frozen --all-extras --group dev --no-install-project --verbose

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
