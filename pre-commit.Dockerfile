FROM 856965016623.dkr.ecr.us-east-1.amazonaws.com/root-mirror/python:3.12-alpine3.22 AS python_packages

ENV COLUMNS=300

# Configure JFrog Alpine repos, then install secure-apk, rootio-patcher, uv, and
# build tools from those hardened repos (not ghcr.io/the public Alpine CDN) -- see
# Dockerfile's python_packages stage for the full rationale.
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
    apk add --no-cache secure-apk uv git build-base

ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./

RUN --mount=type=secret,id=jfrog_read_token \
    set -eu; \
    export UV_INDEX_JFROG_USERNAME=""; \
    export UV_INDEX_JFROG_PASSWORD="$(cat /run/secrets/jfrog_read_token)"; \
    uv sync --frozen --all-extras --group dev --no-install-project --verbose

ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /sourcecode

RUN git config --global --add safe.directory /sourcecode

CMD ["pre-commit", "run", "--all-files"]
