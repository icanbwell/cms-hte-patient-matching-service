export LANG

.PHONY: uv.lock
uv.lock: ## Locks dependencies and updates uv.lock on the local file system
	docker compose --progress=plain build --no-cache --build-arg RUN_UV_LOCK=true dev && \
	CONTAINER_ID=$$(docker create patient-matching-service-dev) && \
	docker cp $$CONTAINER_ID:/tmp/uv.lock uv.lock && \
	docker rm $$CONTAINER_ID

.PHONY:devsetup
devsetup: ## one time setup for devs
	touch .env && \
	make update && \
	make up && \
	make setup-pre-commit && \
	make tests && \
	make up

.PHONY:build
build: ## Builds the docker for dev
	docker compose build --parallel

.PHONY: up
up: ## starts docker containers
	docker compose up --build -d && \
	echo "waiting for patient-matching-service service to become healthy" && \
	while [ "`docker inspect --format {{.State.Health.Status}} patient-matching-service`" != "healthy" ]; do printf "." && sleep 2; done && \
	echo ""
	echo "patient-matching-service Service: http://localhost:5050"

.PHONY: down
down: ## stops docker containers
	docker compose down --remove-orphans

.PHONY:update
update: uv.lock setup-pre-commit  ## Updates all the packages using pyproject.toml
	make build && \
	make run-pre-commit && \
	echo "In PyCharm, do File -> Invalidate Caches/Restart to refresh" && \
	echo "If you encounter issues with remote sources being out of sync, click on the 'Remote Python' feature on" && \
	echo "the lower status bar and reselect the same interpreter and it will rebuild the remote source cache." && \
	echo "See this link for more details:" && \
	echo "https://intellij-support.jetbrains.com/hc/en-us/community/posts/205813579-Any-way-to-force-a-refresh-of-external-libraries-on-a-remote-interpreter-?page=2#community_comment_360002118020"


.DEFAULT_GOAL := help
.PHONY: help
help: ## Show this help.
	# from https://marmelab.com/blog/2016/02/29/auto-documented-makefile.html
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

.PHONY:tests
tests: ## Runs all the tests
	docker compose run --rm --name patient-matching-service_tests dev pytest tests

.PHONY:shell
shell: ## Brings up the bash shell in dev docker
	docker compose run --rm --name patient-matching-service_shell dev /bin/sh

.PHONY:clean-pre-commit
clean-pre-commit: ## removes pre-commit hook
	rm -f .git/hooks/pre-commit

.PHONY:setup-pre-commit
setup-pre-commit:
	cp ./pre-commit-hook ./.git/hooks/pre-commit

.PHONY:run-pre-commit
run-pre-commit: setup-pre-commit
	./.git/hooks/pre-commit pre_commit_all_files
