# iTether - developer targets.
#
# Targets:
#   make test         run the Python unit tests
#   make lint         run pyflakes + import hygiene checks
#   make demo         run the live end-to-end demo (no iPhone required)
#   make cover        run the tests under coverage
#   make clean        remove __pycache__, .pytest_cache, etc.

PYTHON ?= python3
COVERAGE_MIN ?= 60

.PHONY: all test lint demo cover clean

all: test

test:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	$(PYTHON) -m py_compile itether_core/*.py
	$(PYTHON) -m py_compile itether_linux/*.py
	$(PYTHON) -m py_compile tests/*.py

demo:
	PYTHONPATH=. $(PYTHON) -m scripts.itether_demo

cover:
	$(PYTHON) -m coverage run --source=itether_core -m unittest discover -s tests
	$(PYTHON) -m coverage report -m --fail-under=$(COVERAGE_MIN)

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache  -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true
