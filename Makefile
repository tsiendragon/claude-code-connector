# ccc — unified developer Makefile
#
#   make build          Compile TypeScript → dist/
#   make install        Build + install ccc binary globally (npm link)
#   make install-bun    Build with bun and copy binary to PREFIX
#   make dev            Install Python wrapper in editable mode
#   make test           Run Python unit tests
#   make lint           Lint Python (ruff) + TypeScript (tsc --noEmit)
#   make clean          Remove build artefacts

.PHONY: build install install-bun dev test lint fmt clean

PREFIX ?= $(HOME)/.local/bin
PYTHON ?= python
PIP    ?= pip

# ---------------------------------------------------------------------------
# TypeScript build
# ---------------------------------------------------------------------------

build:
	npm run build

install: build
	npm link

# Bun-compiled single binary (faster startup, no node_modules at runtime)
install-bun:
	$(eval BUN := $(shell command -v bun 2>/dev/null || echo $(HOME)/.bun/bin/bun))
	@if [ ! -f "$(BUN)" ]; then curl -fsSL https://bun.sh/install | bash; fi
	$(BUN) install --frozen-lockfile
	$(BUN) build src/cli.ts --compile --outfile ccc-bin
	mkdir -p $(PREFIX)
	cp -f ccc-bin $(PREFIX)/ccc
	chmod 755 $(PREFIX)/ccc
	rm -f ccc-bin
	@echo "Installed: $(PREFIX)/ccc"

# ---------------------------------------------------------------------------
# Python wrapper
# ---------------------------------------------------------------------------

dev:
	$(PIP) install -e ".[dev]" --break-system-packages

test: build
	$(PYTHON) -m pytest tests/unit/ -v

lint:
	$(PYTHON) -m ruff check py/ tests/
	npx tsc --noEmit

fmt:
	$(PYTHON) -m ruff check --fix py/ tests/
	$(PYTHON) -m ruff format py/ tests/

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

clean:
	rm -rf dist/ py/ccc.egg-info py/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
