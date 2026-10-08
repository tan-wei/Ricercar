# ── Ricercar — day-to-day commands ────────────────────────────────────────
#
# `just --list` shows these with their one-line help.
#
# After a fresh clone: `just` (or `just setup`). That installs the Python environment,
# the Node tooling commitlint needs, and the git hooks.
#
# Everything here runs on Linux, macOS and Windows. `just` itself uses `sh` on every
# platform — including Windows, where Git for Windows provides it — so the recipes are
# written once, in `sh`, and only the few things that differ get a `[unix]` and a
# `[windows]` variant (`browser` is the one today).

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
#
# The recipe exists twice, once per platform, because launching a browser *detached* is
# the only thing that differs; `just` picks the variant that matches. Both start it on a
# profile of its own under data/browser_profiles/, which is what Chrome ≥136 requires for
# --remote-debugging-port to be honoured at all.
#
# Any Chromium will do — Chrome, Edge, Brave — and a full path works too, if the default
# is not on your PATH: `just browser chromium` or `just browser "C:/…/chrome.exe" 9333`.

# Launch the browser Ricercar attaches to (CDP on `port`, profile data/browser_profiles/cdp-chrome)
[unix]
browser bin=(if os() == "macos" { "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" } else { "google-chrome" }) port="9222":
    @echo "Starting {{bin}} — CDP on port {{port}}, profile data/browser_profiles/cdp-chrome"
    @nohup "{{bin}}" --remote-debugging-port={{port}} --user-data-dir=data/browser_profiles/cdp-chrome >/dev/null 2>&1 &
    @sleep 4
    @just _browser-check "{{bin}}" {{port}}

# Launch the browser Ricercar attaches to (CDP on `port`, profile data/browser_profiles/cdp-chrome)
[windows]
browser bin="chrome.exe" port="9222":
    @echo "Starting {{bin}} — CDP on port {{port}}, profile data/browser_profiles/cdp-chrome"
    @powershell -NoProfile -Command "Start-Process -FilePath '{{bin}}' -ArgumentList '--remote-debugging-port={{port}}','--user-data-dir=data/browser_profiles/cdp-chrome'"
    @sleep 4
    @just _browser-check "{{bin}}" {{port}}

# Report whether something answers on the CDP port, and what to do if nothing does
[private]
_browser-check bin port:
    @if curl -fsS "http://127.0.0.1:{{port}}/json/version" >/dev/null 2>&1; then \
        echo "Ready on port {{port}} — leave this browser running, and log in once if it asks."; \
    else \
        echo "Nothing answered on port {{port}} — is {{bin}} the right binary, and on PATH?"; \
        echo "Any Chromium will do: just browser chromium   (or a full path, as the first argument)"; \
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

# The suite with coverage for CI: terminal + coverage.xml for Codecov and the artifact
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

# Follow today's log file (the date in the name is the local date a run wrote it)
logs:
    tail -n 50 -F "data/logs/ricercar_{{datetime('%Y-%m-%d')}}.log"

# ── Data ──────────────────────────────────────────────────────────────────

# Import the legacy database into the current schema, in place. Reads database.path from
# the configuration, keeps a copy of it (torrents.db.backup-<timestamp>), rebuilds the
# same file with the constraints the legacy schema was missing, and leaves the name
# alone. A database that is already current has nothing to import and says so.
migrate source="":
    uv run python -m ricercar.repository.migrate "{{source}}"

# ── Clean ─────────────────────────────────────────────────────────────────

# Remove caches, coverage output and __pycache__ (node_modules and .venv are left alone)
clean:
    find . -type d -name __pycache__ -not -path "*/.venv/*" -not -path "*/node_modules/*" -exec rm -rf {} + 2>/dev/null; true
    rm -rf .mypy_cache .ruff_cache .pytest_cache .coverage coverage.xml

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
