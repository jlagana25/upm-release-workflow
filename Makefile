# UPM Release Workflow — common tasks.
# Usage:  make <target>   (run from the files/ directory)

PY ?= python3

.PHONY: help install install-hooks lock security security-history smoke steps verify

help:
	@echo "UPM Release Workflow — make targets:"
	@echo "  make install   Install dependencies (requirements.txt) + Playwright browser"
	@echo "  make lock       Pin the current environment to requirements.lock"
	@echo "  make security   Scan worktree for private auth material"
	@echo "  make security-history  Audit every reachable Git commit"
	@echo "  make smoke      Run the fast offline smoke test"
	@echo "  make steps      Print the canonical workflow step list"
	@echo "  make verify     smoke test + byte-compile every module"

install:
	$(PY) -m pip install -r requirements.txt
	$(PY) -m playwright install chromium
	$(MAKE) install-hooks
	$(PY) auth_manager.py --permissions

install-hooks:
	git config core.hooksPath .githooks
	@echo "Installed repository credential-scan pre-commit hook."

# Pin exact versions from the CURRENT (known-good) environment.
# Run this on the canonical machine after a clean `make install`, then commit
# requirements.lock and `pip install -r requirements.lock` on the other machine
# so both Macs run identical library versions.
lock:
	$(PY) -m pip freeze > requirements.lock
	@echo "Wrote requirements.lock — commit it so both machines pin the same versions."

smoke:
	$(PY) smoke_test.py

security:
	$(PY) security_scan.py

security-history:
	$(PY) security_scan.py --history

steps:
	$(PY) upm_release_workflow.py --list-steps

verify: security smoke
	$(PY) -m py_compile *.py
	@echo "All modules byte-compile cleanly."
