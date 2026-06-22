# sandbox — task runner
# Run `just` with no args to see all recipes.

default: check

# Install/refresh deps from lockfile
sync:
    uv sync

# Run tests with coverage
test:
    uv run pytest

# Run a single test file or node (e.g. `just test-one tests/test_lifecycle.py::test_ensure_vm_reuses_existing`)
test-one target:
    uv run pytest {{target}} -o addopts="-ra --no-cov"

# Lint: ruff check + mypy strict
lint:
    uv run ruff check .
    uv run mypy

# Auto-fix lint issues + format
fix:
    uv run ruff check . --fix
    uv run ruff format .

# Format only (no auto-fix)
fmt:
    uv run ruff format .

# End-to-end specdown tests (invokes `hermes sandbox` CLI via a throwaway
# HERMES_HOME).  Installs specdown if not on PATH, then runs the specs in docs/.
# Cloud tokens (HCLOUD_TOKEN, DIGITALOCEAN_TOKEN) are forwarded from the
# environment — when absent, the corresponding provider task spec is skipped.
specdown:
    @command -v specdown >/dev/null 2>&1 || { \
        echo "specdown not found — installing latest release to a temp dir"; \
        dir="$$(mktemp -d)"; \
        curl -L https://github.com/specdown/specdown/releases/latest/download/specdown-x86_64-unknown-linux-gnu \
            -o "$$dir/specdown"; \
        chmod +x "$$dir/specdown"; \
        export PATH="$$dir:$$PATH"; \
        ./test-harness/specdown; \
        exit $$?; \
    }
    ./test-harness/specdown

# Full gate: lint + test + end-to-end (run before pushing/releasing)
check: lint test specdown
    @echo "✓ all green"

# Show line coverage in terminal (no HTML)
coverage:
    uv run pytest --cov-report=term-missing

# Generate HTML coverage report at htmlcov/index.html
coverage-html:
    uv run pytest --cov-report=html
    @echo "→ open htmlcov/index.html"

# Clean all caches and build artifacts
clean:
    rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov *.egg-info build dist

# Reinstall from scratch (nuke venv + relock)
reinstall:
    rm -rf .venv uv.lock
    uv sync

# ── Cocogitto (conventional commits + releases) ─────────────────────────────

# Verify the repo's commit history is conventional-compliant
cog-check:
    cog check

# Show the current determined version (from tags + commit history)
cog-version:
    cog get-version

# Make a conventional commit (e.g. `just cog-commit feat "add foo"`)
cog-commit type msg:
    cog commit {{type}} "{{msg}}"

# Install cocogitto git hooks (commit-msg verification + git-mit) — run once
cog-install-hooks:
    cog install-hook -a -o
    # cocogitto 7.0 misnames prepare-commit-msg as pre-commit-msg; fix it.
    @if [ -f .git/hooks/pre-commit-msg ] && [ ! -f .git/hooks/prepare-commit-msg ]; then \
        mv .git/hooks/pre-commit-msg .git/hooks/prepare-commit-msg; \
        echo "fixed prepare-commit-msg hook name"; \
    fi

# Generate/refresh CHANGELOG.md from commit history
cog-changelog:
    cog changelog

# Show what the next auto-version would be, without changing anything
cog-dry-run:
    cog bump --auto --dry-run

# Cut a release: auto-bump version, update CHANGELOG.md, create tag
release:
    cog bump --auto

# Cut a release with an explicit version (e.g. `just release-version 1.0.0`)
release-version version:
    cog bump --version {{version}}

# Push release commits + tags to origin (run after `just release`)
push:
    git push
    git push origin --tags

# ── PyPI publishing ──────────────────────────────────────────────────────────

# Build sdist + wheel into dist/
build:
    uv build
    uv run twine check dist/*

# Publish to TestPyPI (https://test.pypi.org) — requires TWINE_PASSWORD or
# API token in ~/.pypirc.  Uses --skip-existing so re-running after a partial
# upload won't 400 on already-present files.
publish-test:
    uv run twine upload --repository testpypi --skip-existing dist/*

# Publish to PyPI (https://pypi.org).  Run `just build` or `just release`
# first so dist/ has fresh artifacts matching the tagged version.
publish:
    uv run twine upload --skip-existing dist/*
