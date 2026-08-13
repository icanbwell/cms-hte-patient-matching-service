# Root.io/JFrog Migration — learnings from this session (patient-matching-service)

## 1. Same `explicit=true`/empty-`[tool.uv.sources]` gap as the sibling repo
Identical to `patient-matching`: a JFrog index was already configured but `explicit =
true` with nothing mapped in `[tool.uv.sources]` — nothing actually routed through it.
Switched to `default = true`. Worth checking for on every repo that already shows
`using_jfrog: YES` in the tracker; it's evidently a common half-done state across this
org's earlier JFrog setup attempts, not a one-off.

## 2. A *second* Dockerfile needed migrating: `pre-commit.Dockerfile`
This repo runs lint/type-check/format via a separate, dedicated `pre-commit.Dockerfile`
(built and run by the `pre-commit-hook` shell script, invoked via `make run-pre-commit`
in CI) — not the main app Dockerfile. It also pulled `uv` from `ghcr.io` and had zero
JFrog wiring. Once the main `pyproject.toml`'s JFrog index became `default = true`, this
second Dockerfile's `uv sync` would have started needing JFrog auth to succeed at all
and had none. Migrated it identically to the main Dockerfile, and updated
`pre-commit-hook` to pass `--secret id=jfrog_read_user,env=JFROG_READ_USER --secret
id=jfrog_read_token,env=JFROG_READ_TOKEN` to its `docker build` call (which previously
had no `--secret` flags of any kind). **Check for a second/lint-only Dockerfile before
assuming the app Dockerfile is the only thing that needs migrating** — this pattern
(dedicated pre-commit image, `language: system` pre-commit hooks) may recur on other
repos in this org.

## 3. Pre-existing, partially-wired JFrog secrets in `docker-publish.yml` — with the wrong secret name
This repo's `docker-publish.yml` already had `secrets: | "jfrog_token=${{
secrets.JFROG_READ_TOKEN }}"` on both its `build-amd64` and `build-arm64` jobs — a prior,
incomplete migration attempt. Two problems: it passed only the token, never the
username, and it used the secret ID `jfrog_token`, which doesn't match the
`jfrog_read_user`/`jfrog_read_token` convention this repo's own Dockerfile (and every
other migrated repo) actually expects. This half-finished state is exactly the kind of
thing `using_jfrog: NO` in the tracker was correctly signaling — but it also means the
existing code isn't a blank slate to build on; check what's already there for
partial/mismatched wiring, not just whether wiring exists at all.

## 4. Don't validate a Docker image's test suite with a bare `docker run` — it skips the volume mount
Running `docker run --rm patient-matching-service-dev pytest tests` directly gave 2
tests, one failing with "async def functions are not natively supported" even though
`pytest-asyncio` was genuinely installed in the image. Root cause: the `production`/
`development` stages never `COPY` `pyproject.toml` into the image at all (only the
app package and `tests/` are copied) — so there's no `[tool.pytest.ini_options]` for
pytest to find, and `asyncio_mode = "auto"` never activates. CI's actual invocation
(`docker compose run --rm dev pytest . ...`) works because `docker-compose.yml`'s
`volumes: - ./:/usr/src/patient_matching_service/` bind-mounts the *entire* live repo
(including `pyproject.toml`) over whatever the image has baked in. Re-ran via
`docker compose run` instead of a bare `docker run` and got the real result: 2 passed.
**Always validate a Dockerfile change through the same invocation CI actually uses**
(`docker compose run`, not `docker run` against the raw built image) when the compose
file relies on a bind mount — the image alone is not a faithful stand-in.

---

Claude: update the Root.io/JFrog Migration Guide
(https://icanbwell.atlassian.net/wiki/spaces/CIE/pages/6195544074/Root.io+Jfrog+Migration+Guide)
with these notes, then delete this file.
