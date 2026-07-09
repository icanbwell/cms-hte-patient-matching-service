FROM public.ecr.aws/docker/library/python:3.12-alpine3.20 AS python_packages

ENV COLUMNS=300

COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /uvx /usr/local/bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

RUN apk add --no-cache git build-base

COPY pyproject.toml uv.lock* ./

RUN uv sync --frozen --all-extras --group dev --no-install-project --verbose

ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /sourcecode

RUN git config --global --add safe.directory /sourcecode

CMD ["pre-commit", "run", "--all-files"]
