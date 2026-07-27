# Convenience targets. `deploy.sh` is the real entry point for deployment;
# these wrap the things you run dozens of times a day while developing.

.DEFAULT_GOAL := help
SHELL := /bin/bash

REGION ?= eu-west-2
ENV    ?= dev
STACK  := fplbot-$(ENV)

# The test suite reads settings from the environment. These are obvious
# placeholders - the suite never talks to AWS.
TEST_ENV := PYTHONPATH=src \
            TABLE_NAME=test-table \
            BUCKET_NAME=test-bucket \
            EMAIL_FROM=test@example.com \
            EMAIL_TO=test@example.com \
            SEASON=2026-27 \
            AWS_DEFAULT_REGION=$(REGION) \
            POWERTOOLS_TRACE_DISABLED=1

.PHONY: help
help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install:  ## Install runtime and development dependencies
	python -m pip install --upgrade pip
	pip install -r layer/requirements.txt
	pip install -e ".[dev]"

.PHONY: test
test:  ## Run the test suite
	$(TEST_ENV) pytest -m "not live" -q

.PHONY: cov
cov:  ## Run tests with a coverage report
	$(TEST_ENV) pytest -m "not live" --cov=src/fplbot --cov-report=term-missing

.PHONY: lint
lint:  ## Lint and check formatting
	ruff check src tests
	ruff format --check src tests

.PHONY: fmt
fmt:  ## Auto-fix lint and apply formatting
	ruff check --fix src tests
	ruff format src tests

.PHONY: types
types:  ## Type-check
	mypy src

.PHONY: validate
validate:  ## Validate the CloudFormation templates
	sam validate --template template.yaml --lint
	cfn-lint template.yaml pipeline/pipeline.yaml

.PHONY: fixtures
fixtures:  ## Rebuild the hand-crafted test fixtures
	python scripts/record_fixtures.py --synthetic-only

.PHONY: record
record:  ## Record live API responses (hits third-party APIs)
	python scripts/record_fixtures.py

.PHONY: build
build:  ## sam build, in a Lambda-compatible container
	sam build --template template.yaml --parallel --use-container \
		--build-image public.ecr.aws/sam/build-python3.13:latest-arm64

.PHONY: deploy
deploy:  ## Deploy via deploy.sh (ENV=dev|prod)
	./deploy.sh --env $(ENV) --region $(REGION)

.PHONY: invoke
invoke:  ## Invoke the deployed poll function once
	aws lambda invoke --function-name fplbot-poll-$(ENV) --region $(REGION) /dev/stdout

.PHONY: logs
logs:  ## Tail the deployed function's logs
	sam logs --stack-name $(STACK) --region $(REGION) --tail

.PHONY: local
local:  ## Invoke locally with SAM (dry-run)
	sam local invoke PollFunction --event events/scheduled.json

.PHONY: check
check: lint types test  ## Everything CI runs

.PHONY: clean
clean:  ## Remove build and cache artefacts
	rm -rf .aws-sam .pytest_cache .mypy_cache .ruff_cache htmlcov \
	       .coverage coverage.xml test-results.xml packaged.yaml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
