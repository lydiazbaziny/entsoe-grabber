.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash

# Independent Terraform stacks, each with its own state. Deploy them in this
# order and destroy in reverse: app looks up what network and storage created.
STACKS := network storage app
TF = terraform -chdir=infra/$(STACK)

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- python ------------------------------------------------------------------

.PHONY: sync
sync: ## Install/refresh the virtualenv
	uv sync --all-extras

.PHONY: fmt
fmt: ## Format Python and Terraform
	uv run ruff format .
	uv run ruff check --fix .
	terraform -chdir=infra fmt -recursive

.PHONY: lint
lint: ## Lint and type-check the Python code
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

# Separate from `lint`: validate needs providers installed and the zip on
# disk, because the app stack hashes it with filebase64sha256. -backend=false
# installs providers without credentials or touching remote state.
.PHONY: tf-lint
tf-lint: build ## Check formatting and validity of every Terraform stack
	terraform -chdir=infra fmt -check -recursive
	@for stack in $(STACKS); do \
		terraform -chdir=infra/$$stack init -backend=false -input=false >/dev/null && \
		terraform -chdir=infra/$$stack validate || exit 1; \
	done

.PHONY: tf-test
tf-test: build ## Test the app stack with mocked providers (no AWS resources)
	terraform -chdir=infra/app init -backend=false -input=false >/dev/null
	terraform -chdir=infra/app test

.PHONY: check
check: lint test ## Everything that runs without AWS or a build

.PHONY: test
test: ## Run the test suite
	uv run pytest --cov=entsoe_grabber --cov-report=term-missing

# Excluded from `check` on purpose: these reach the live IOP platform, so they
# need a token and network, and an empty IOP dataset can legitimately skip them.
.PHONY: smoke
smoke: ## Run the live smoke tests against the ENTSO-E IOP environment
	uv run pytest -m smoke

# --- packaging ---------------------------------------------------------------

.PHONY: build
build: ## Build the Lambda function zip
	./scripts/build.sh

.PHONY: clean
clean: ## Remove build artifacts
	rm -rf build .pytest_cache .ruff_cache .mypy_cache .coverage .coverage_files
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --- terraform ---------------------------------------------------------------
# Every target here acts on one stack: make plan STACK=app

.PHONY: require-stack
require-stack:
	@case " $(STACKS) " in *" $(STACK) "*) ;; \
		*) echo "Set STACK to one of: $(STACKS) (e.g. make plan STACK=app)" >&2; exit 1 ;; \
	esac

.PHONY: tf-init
tf-init: require-stack ## Initialize STACK against the S3 backend in infra/backend.hcl
	$(TF) init -input=false -backend-config=../backend.hcl

# Only the app stack packages the Lambda zip, so only it waits for a build.
.PHONY: plan
plan: require-stack $(if $(filter app,$(STACK)),build) ## Show what would change in AWS for STACK
	$(TF) plan -input=false

.PHONY: deploy
deploy: require-stack $(if $(filter app,$(STACK)),build) ## Apply STACK to AWS (prompts for confirmation)
	$(TF) apply -input=false

.PHONY: destroy
destroy: require-stack ## Tear down the AWS resources of STACK
	$(TF) destroy -input=false

# --- operations --------------------------------------------------------------

.PHONY: whoami
whoami: ## Verify AWS credentials reached the container
	aws sts get-caller-identity

.PHONY: invoke
invoke: ## Invoke the deployed function once and print the response
	@fn=$$(terraform -chdir=infra/app output -raw function_name) && \
	aws lambda invoke --function-name "$$fn" --cli-binary-format raw-in-base64-out \
		--payload '{}' /tmp/entsoe-response.json >/dev/null && \
	cat /tmp/entsoe-response.json && echo

.PHONY: logs
logs: ## Tail the Lambda's CloudWatch logs
	@terraform -chdir=infra/app output -raw log_group | xargs -I{} aws logs tail {} --follow
