# autosentry developer tasks
#
# Conventions: every target is .PHONY; no implicit deps; everything runs
# through uv when available so contributors don't have to remember to
# activate a venv.

PY ?= python3
UV := $(shell command -v uv 2>/dev/null)

# iCloud Drive (and other syncing filesystems) corrupt Python virtualenvs by
# duplicating files and setting UF_HIDDEN on dotfiles. If this repo is under
# such a path, force the venv outside it. Override by setting VENV explicitly.
ifeq ($(VENV),)
    IS_ICLOUD := $(findstring CloudDocs,$(CURDIR))
    ifneq ($(IS_ICLOUD),)
        VENV := $(HOME)/.cache/autosentry-venv
    else
        VENV := .venv
    endif
endif
export UV_PROJECT_ENVIRONMENT := $(VENV)

ifeq ($(UV),)
    RUN := $(PY) -m
    PIP := $(PY) -m pip
    INSTALL_DEV := $(PIP) install -e ".[dev]"
else
    RUN := uv run
    PIP := uv pip
    INSTALL_DEV := uv sync --all-extras
endif

.PHONY: help install hooks format lint lint-fix typecheck test test-cov ci clean build venv-info

help:                  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install:               ## Install package + dev deps (uses uv if available)
	@echo ">> venv: $(VENV)"
	$(INSTALL_DEV)
	@# Auto-install the pre-commit hook when pre-commit is available and
	# the repo is a git checkout. Quiet on success; a no-op when missing.
	@if [ -d .git ] && $(RUN) pre-commit --version >/dev/null 2>&1; then \
	    $(RUN) pre-commit install --install-hooks >/dev/null && \
	    echo ">> pre-commit hooks installed (.git/hooks/pre-commit)"; \
	fi

hooks:                 ## Install pre-commit hooks (idempotent)
	$(RUN) pre-commit install --install-hooks

venv-info:             ## Print the venv path the Makefile will use
	@echo "$(VENV)"

format:                ## Format with ruff
	$(RUN) ruff format src tests

lint:                  ## Lint (no autofix)
	$(RUN) ruff check src tests
	$(RUN) ruff format --check src tests

lint-fix:              ## Lint with autofix
	$(RUN) ruff check --fix src tests
	$(RUN) ruff format src tests

typecheck:             ## Run pyrefly
	$(RUN) pyrefly check src/autosentry

test:                  ## Run tests
	$(RUN) pytest

test-cov:              ## Run tests with coverage
	$(RUN) pytest --cov=autosentry --cov-report=term-missing --cov-report=xml

ci: lint typecheck test ## What CI runs

build:                 ## Build sdist + wheel
	$(RUN) python -m build

clean:                 ## Remove build artifacts and caches
	rm -rf build dist *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +
	find . -type d -name .ruff_cache -prune -exec rm -rf {} +
	rm -rf .coverage coverage.xml htmlcov
