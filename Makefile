# ---------------------------------------------------------------------------
# claude-cli-connector — developer Makefile
# ---------------------------------------------------------------------------
# Usage:
#   make test        Run unit tests (default)
#   make test-all    Run unit + integration tests (requires tmux + claude CLI)
#   make cov         Run unit tests with coverage report
#   make lint        Run ruff linter
#   make fmt         Auto-fix style issues with ruff
#   make typecheck   Run mypy static type checker
#   make build       Build source + wheel distributions
#   make publish     Upload to PyPI (requires twine + valid credentials)
#   make clean       Remove build/dist/cache artefacts
#   make install-dev Install package in editable mode with dev dependencies
# ---------------------------------------------------------------------------

.PHONY: test test-all cov lint fmt typecheck build publish clean install-dev

# Python / pip to use (override with: make test PYTHON=python3.11)
PYTHON ?= python
PIP    ?= pip

# Directories
SRC_DIR   = src
TEST_DIR  = tests
DIST_DIR  = dist

# ---------------------------------------------------------------------------
# Development setup
# ---------------------------------------------------------------------------

install-dev:
	$(PIP) install -e ".[dev]" --break-system-packages

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test:
	$(PYTHON) -m pytest $(TEST_DIR)/unit/ -v

test-all:
	$(PYTHON) -m pytest $(TEST_DIR)/unit/ $(TEST_DIR)/integration/ --run-integration -v

cov:
	$(PYTHON) -m pytest $(TEST_DIR)/unit/ \
	    --cov=$(SRC_DIR)/claude_cli_connector \
	    --cov-report=term-missing \
	    --cov-report=html:htmlcov \
	    -v

# ---------------------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------------------

lint:
	$(PYTHON) -m ruff check $(SRC_DIR) $(TEST_DIR)

fmt:
	$(PYTHON) -m ruff check --fix $(SRC_DIR) $(TEST_DIR)
	$(PYTHON) -m ruff format $(SRC_DIR) $(TEST_DIR)

typecheck:
	$(PYTHON) -m mypy $(SRC_DIR)

# Run all checks in sequence (used in CI / pre-push)
check: lint typecheck test

# ---------------------------------------------------------------------------
# Build & publish
# ---------------------------------------------------------------------------

build: clean
	$(PYTHON) -m build

publish: build
	$(PYTHON) -m twine upload $(DIST_DIR)/*

# Publish to TestPyPI for validation before a real release
publish-test: build
	$(PYTHON) -m twine upload --repository testpypi $(DIST_DIR)/*

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

clean:
	rm -rf $(DIST_DIR) build *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name ".coverage" -delete 2>/dev/null || true
