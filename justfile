# ── Ricercar — day-to-day commands ────────────────────────────────────────
#
# `just --list` shows these with their one-line help.
#
# After a fresh clone: `just` (or `just setup`). That installs the Python environment,
# the Node tooling commitlint needs, and the git hooks.

set positional-arguments := true

# ── Setup ─────────────────────────────────────────────────────────────────

# The whole local toolchain: Python, commitlint and the git hooks (also the default recipe)
setup: sync setup-node install-hooks

# Alias of `setup`
default: setup

# The Python environment: the project, the dev group, and a lock check
sync:
    uv sync --group dev

# Install the Node tooling (commitlint) into node_modules/, where `npm exec --no` finds it
setup-node:
    npm ci

# Git hooks: ruff (pre-commit) and commitlint (commit-msg)
install-hooks:
    uv run pre-commit install --hook-type pre-commit --hook-type commit-msg

# Remove the git hooks again
uninstall-hooks:
    uv run pre-commit uninstall --hook-type pre-commit --hook-type commit-msg

# NOTE: there is deliberately no `playwright install` step — Playwright's own browsers
# are never used. Attach to a browser you launched yourself instead (see README.md).

# ── The browser (the one prerequisite for a real run) ─────────────────────

# Any Chromium will do: `just browser msedge.exe` (or `just browser "<path to chrome>" 9333`)
# Launch the browser Ricercar attaches to: CDP on `port`, its own profile directory
browser bin="chrome.exe" port="9222":
    @echo "Starting {{bin}} — CDP on port {{port}}, profile data/browser_profiles/cdp-chrome"
    @powershell -NoProfile -Command "Start-Process -FilePath '{{bin}}' -ArgumentList '--remote-debugging-port={{port}}','--user-data-dir=data/browser_profiles/cdp-chrome'"
    @sleep 4
    @if curl -fsS "http://localhost:{{port}}/json/version" >/dev/null 2>&1; then \
        echo "Ready on port {{port}} — leave this browser running, and log in once if asked."; \
    else \
        echo "Nothing answered on port {{port}} — is {{bin}} on PATH?"; \
        echo "A full path works too: just browser \"C:/Program Files/Google/Chrome/Application/chrome.exe\""; \
    fi

# ── Quality ───────────────────────────────────────────────────────────────

# Lint
lint:
    uv run ruff check src/ tests/ scripts/

# Format (writes the changes)
format:
    uv run ruff format src/ tests/ scripts/

# Format check (what CI runs)
format-check:
    uv run ruff format --check src/ tests/ scripts/

# Type-check (the target is `files` in pyproject.toml: src/)
typecheck:
    uv run mypy

# Everything the `quality` CI job runs, in one command
check: lint format-check typecheck

# ── Commit messages ───────────────────────────────────────────────────────
#
# Conventional Commits, sentence-case subjects (commitlint.config.cjs). The same check
# runs in the `commits` CI job and from the `commit-msg` hook `just setup` installs.
#
# `just setup-node` puts commitlint in node_modules/, and these recipes invoke it with
# `node` directly, so nothing here needs a global npm install.

# Check the message of the last commit
commitlint:
    git log -1 --format=%B | node node_modules/@commitlint/cli/cli.js

# Check a message without committing: `just commitlint-message 'feat: Add a thing'`
commitlint-message message:
    printf '%s\n' '{{message}}' | node node_modules/@commitlint/cli/cli.js

# Check every commit in a range — what CI does for a push or a pull request. A branch's
# first push (the zero sha) and a force-push have no range to walk; the workflow checks
# ancestry first and falls back to `just commitlint` for the tip
commitlint-range from to:
    node node_modules/@commitlint/cli/cli.js --from {{from}} --to {{to}}

# ── Test ──────────────────────────────────────────────────────────────────

# The whole suite; extra arguments go to pytest (`just test tests/test_parser.py -k piece`)
test *args="":
    uv run pytest -v {{args}}

# Only the browser-marked tests: they run when a browser is attached, skip otherwise
test-integration:
    uv run pytest -v -m integration

# The suite with coverage in the terminal
test-cov:
    uv run pytest -v --cov=src/ricercar --cov-report=term-missing

# The suite with coverage for CI: terminal + coverage.xml for the artifact
coverage:
    uv run pytest --cov=src/ricercar --cov-report=term-missing --cov-report=xml:coverage.xml

# Everything the CI workflow runs, in one command
ci: check test

# ── Run ───────────────────────────────────────────────────────────────────

# Run on the schedule — the "leave it running" mode (Ctrl-C once stops the cycle, twice leaves)
run *args="":
    uv run python -m ricercar {{args}}

# One cycle and exit; extra arguments go through, e.g. `just run-once --limit 1`
run-once *args="":
    uv run python -m ricercar --once {{args}}

# One cycle against the local fixtures: no network, no login, no quota spent
offline *args="":
    uv run python -m ricercar --once --offline tests/fixtures {{args}}

# The resolved configuration (secrets included — it is your own local file)
config *args="":
    uv run python -m ricercar show-config {{args}}

# Send one test notification to the configured notify.urls
notify-test:
    uv run python -m ricercar notify-test

# What each task has yielded, worst first (`--stale`, `--all`, `--json`, `--prune`)
tasks *args="":
    uv run python -m ricercar tasks {{args}}

# Follow today's log file
logs:
    tail -F "data/logs/ricercar_$(date +%F).log"

# ── Data ──────────────────────────────────────────────────────────────────

# The copy is staged next to the target and swapped in only after it verifies, so this
# is also the way to upgrade a database that is still on the legacy schema.
# Import a legacy database into the current schema
migrate source="legacy/torrents.db":
    uv run python -m ricercar.repository.migrate "{{source}}"

# ── Clean ─────────────────────────────────────────────────────────────────

# Remove caches and __pycache__
clean:
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
    rm -rf .mypy_cache .ruff_cache

# `clean`, plus the runtime data — including the signed-in browser profile
clean-all: clean
    # WARNING: data/browser_profiles/ holds the signed-in browser profile (deleting it
    # means logging in by hand again), and data/task_history.json holds months of
    # knowledge about which tasks still find anything.
    rm -rf data/ logs/ Torrents/

# ── Pre-commit ────────────────────────────────────────────────────────────

# Run the hooks over the whole tree
precommit:
    uv run pre-commit run --all-files

# Alias for `install-hooks`, kept because it reads better next to `precommit`
precommit-install: install-hooks
