.DEFAULT_GOAL := help
.NOTPARALLEL:
DOCKER ?= docker
PROJECT_NAME ?= mailbox-dashboard
NODE_IMAGE ?= node:22-alpine
export DOCKER PROJECT_NAME NODE_IMAGE

.PHONY: help setup doctor doctor-deps up start down stop restart build logs status health demo tools test lint check coverage

help: ## Show the supported workflow commands
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-14s %s\n", $$1, $$2}' Makefile

setup: ## Install team, prepare the mailbox and build the dashboard
	@sh scripts/workflow.sh setup

doctor: ## Check team, PATH, Docker and mailbox configuration without changes
	@sh scripts/workflow.sh doctor

doctor-deps: ## Check Docker CLI, Compose and daemon only
	@sh scripts/workflow.sh doctor-deps

up: ## Build current source and start at http://localhost:17171
	@sh scripts/workflow.sh up

start: up ## Alias for up

down: ## Stop this mailbox's containers
	@sh scripts/workflow.sh down

stop: down ## Alias for down

restart: ## Rebuild and recreate the dashboard with current source
	@sh scripts/workflow.sh restart

build: ## Build the dashboard image
	@sh scripts/workflow.sh build

logs: ## Follow the dashboard logs (Ctrl-C to detach)
	@sh scripts/workflow.sh logs

status: ## Show the dashboard's container status
	@sh scripts/workflow.sh status

health: ## Check the running dashboard's HTTP health endpoint
	@sh scripts/workflow.sh health

demo: ## Start with synthetic data without changing .env
	@sh scripts/workflow.sh demo

tools: ## Install the isolated Python test and coverage environment
	@sh scripts/workflow.sh tools

test: tools ## Run all tests in an isolated container
	@sh scripts/workflow.sh test

lint: ## Check JavaScript and shell syntax in isolated environments
	@sh scripts/workflow.sh lint

check: test lint ## Run tests and syntax checks

coverage: tools ## Run tests with coverage; reports in .coverage-output/
	@sh scripts/workflow.sh coverage
