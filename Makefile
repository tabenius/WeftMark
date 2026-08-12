PYTHON ?= python3

.PHONY: all docs html pdf figures logo tasks rev0 clean

all: docs logo tasks

docs: html pdf

figures:
	$(PYTHON) scripts/build_figures.py

html:
	$(PYTHON) scripts/build_html.py

pdf:
	$(PYTHON) scripts/build_pdf.py

logo:
	$(PYTHON) scripts/build_logo.py --size 512

tasks:
	$(PYTHON) scripts/validate_tasks.py

rev0: figures logo pdf
	mkdir -p docs/artifacts
	cp build/weftmark.html docs/artifacts/weftmark_rev0.html
	cp build/weftmark_A5.pdf docs/artifacts/weftmark_rev0.pdf

clean:
	rm -rf build/* assets/figures/* assets/generated/*
