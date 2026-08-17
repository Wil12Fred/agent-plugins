.DEFAULT_GOAL := help
UV ?= uv

# Every package is independent: its own pyproject, its own tests. The apps share
# `opscore` through the uv workspace; the plugin does not, because a Claude Code
# plugin is installed by copying its directory and has to carry its own.
APPS    := apps/opscore apps/cloudprobe apps/gpull apps/jiractl apps/slack-bridge
PLUGINS := plugins/agent-toolkit
ALL     := $(APPS) $(PLUGINS)

.PHONY: help sync test lint fmt typecheck validate check integration install-claude install-agents clean

help: ## Show the available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

sync: ## Install every package
	@set -e; for p in $(ALL); do echo "── $$p"; (cd $$p && $(UV) sync --quiet --all-extras); done

test: ## Run every offline suite. Integration is opt-in; see `make integration`
	@set -e; for p in $(ALL); do printf "%-26s " "$$p"; (cd $$p && $(UV) run pytest -q | tail -1); done

integration: ## Run the suites that touch this machine and the network
	@set -e; for p in apps/slack-bridge apps/jiractl; do \
	  printf "%-26s " "$$p"; (cd $$p && $(UV) run pytest -m integration -q | tail -1); done

lint: ## Ruff over every package
	@set -e; for p in $(ALL); do printf "%-26s " "$$p"; (cd $$p && $(UV) run ruff check src tests --output-format=concise | tail -1); done

fmt: ## Ruff format (writes)
	@set -e; for p in $(ALL); do (cd $$p && $(UV) run ruff format src tests --quiet && $(UV) run ruff check src tests --fix --quiet) || true; done

typecheck: ## mypy --strict over every package
	@set -e; for p in $(ALL); do printf "%-26s " "$$p"; (cd $$p && $(UV) run mypy src | tail -1); done

validate: ## Every plugin manifest still parses
	@set -e; for p in plugins/*/; do printf "%-26s " "$$p"; claude plugin validate "$$p" | tail -1; done

check: lint typecheck test validate ## Everything CI runs
	@echo "check: all packages clean"

install-claude: ## Install the plugins into Claude Code (run from your own terminal)
	cd "$(HOME)" && claude plugin marketplace add "$(CURDIR)"
	cd "$(HOME)" && claude plugin install agent-toolkit@agent-plugins --scope user
	cd "$(HOME)" && claude plugin install workstation@agent-plugins --scope user

install-agents: ## Link the skills for Codex and Gemini, which have no plugin system
	@# Both read ~/.agents/skills, so one set of links covers them.
	mkdir -p $(HOME)/.agents/skills
	@set -e; for s in $(CURDIR)/plugins/*/skills/*/; do \
	  ln -sfn "$$s" "$(HOME)/.agents/skills/$$(basename $$s)"; done
	@ls $(HOME)/.agents/skills | sed 's/^/  /'

clean: ## Remove caches
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	find . \( -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache \) -prune -exec rm -rf {} + 2>/dev/null || true
