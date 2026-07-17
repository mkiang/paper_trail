# paper_trail — common tasks. Run `make help` for the list.
VENV = .venv
PY = $(VENV)/bin/python
PIP = $(VENV)/bin/pip

.PHONY: help venv install test lint fmt fmt-check build example-pdf init doctor clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  %-14s %s\n", $$1, $$2}'

venv:  ## Create the project virtualenv if missing
	@test -d $(VENV) || python3 -m venv $(VENV)

install: venv  ## Install the package + dev (test/lint) tooling, editable
	$(PIP) install -U pip
	$(PIP) install -e ".[dev]"

test:  ## Run the pytest suite
	$(PY) -m pytest

lint:  ## Report lint issues (ruff, no autofix)
	$(PY) -m ruff check scripts tests

fmt:  ## Format the code (ruff format; preserves quote style)
	$(PY) -m ruff format scripts tests

fmt-check:  ## Check formatting without writing (CI-friendly)
	$(PY) -m ruff format --check scripts tests

build:  ## Build every PDF variant (regenerates publications.bib first)
	./build.sh

example-pdf:  ## Compile the data/example/ sample corpus to output/example-*.pdf
	$(PY) scripts/build_example.py

init:  ## Scaffold a blank CV data tree (refuses non-empty data/; see --force)
	$(PY) scripts/init_cv.py

doctor:  ## Check the environment (typst binary, core deps, data files)
	$(PY) scripts/doctor.py

clean:  ## Remove caches + build tmp files (keeps output PDFs)
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
