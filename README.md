# Ricercar

[![CI](https://github.com/tan-wei/Ricercar/actions/workflows/ci.yml/badge.svg)](https://github.com/tan-wei/Ricercar/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/tan-wei/Ricercar/branch/main/graph/badge.svg)](https://codecov.io/gh/tan-wei/Ricercar)

A torrent monitoring & downloading tool. It watches one or more trackers for new torrents matching your search tasks, downloads the new `.torrent` files into a local directory, records them in an SQLite database, and notifies you when anything is found.

Ricercar drives a **browser you launch yourself** (see [Cloudflare](#cloudflare)) through Playwright's CDP client — no headless browser fingerprinting, no bundled browser — and is designed from the ground up so that additional trackers can be added without touching the shared pipeline.

## Name

"recercar" (also "ricercare") is a late-Renaissance / Baroque instrumental form whose name literally means *to seek / to search out* (see the [Wikipedia article](https://en.wikipedia.org/wiki/Ricercare)). It is also closely tied to the classical-music repertoire this tool was built to watch for — a fitting name for a project that searches out torrents of classical recordings.

---

## Origins

Ricercar started as a rewrite of a legacy tool that scraped rutracker.org with Selenium. That tool was written for a single tracker, hard-coded its page layout, and ran on a fix-it-when-it-breaks basis.

The migration kept the *behaviour* — the same search tasks, the same daily quotas, the same notification shape — but replaced the implementation. It was moved off Selenium and onto Playwright's CDP attach, given a source-agnostic architecture, a proper configuration system, and real failure diagnostics.

## Why it was rewritten

The legacy Selenium solution worked, but was brittle in ways that made it painful to maintain. The rewrite addressed the structural problems:

- **Selenium's browser automation was the problem, not the solution.** Real-world trackers sit behind Cloudflare and fingerprint automation. A browser a human launches by hand is the one client that gets through, so Ricercar attaches to a browser **you** started — Playwright bundles no browser and never launches one.
- **The legacy tool hard-coded a single tracker.** Ricercar's [pipeline](#architecture) talks to trackers only through a small protocol; a second tracker is an additive change.
- **Selectors were scattered through the code.** They now live in one place per source and are overridable from configuration.
- **Failures were silent.** Ricercar grades failures (transient vs. site-changed) and saves evidence — the page HTML, a screenshot, a Playwright trace — for the ones you have to look at.
- **The database had no constraints.** The legacy schema is imported and rebuilt with a primary key and MD5 uniqueness so the same torrent is never stored twice.

## Architecture

```
CLI / Scheduler
  (`ricercar`, `--once` mode, APScheduler background runner)
        │ builds
        ▼
Runner (pipeline)
  - expands tasks into searches (per-source)
  - drives each source in two tabs (results + downloads)
  - paces requests with the configured quota / retry policy
  - dedupes against the DB, respects the daily per-source quota
  - handles interrupts gracefully & resumes on the next run
  - notifies start/finish, flags stale tasks, saves failure evidence
        │
        ├──▶ Source (protocol) ──▶ per-tracker adapters ──▶ browser you launched
        │                          (RutrackerSource: session/login, search,
        │                           topic/download, page diagnosis — selectors
        │                           in one place, overrideable)
        │
        ├──▶ TorrentRepository (SQLite DB — dedup, quota)
        ├──▶ StateStore (state.json — resume state)
        ├──▶ TaskHistory (task_history.json — yields over time)
        └──▶ Notifier (apprise — email, Discord, Telegram, Slack, …)
```

The adapters drive a browser **you** launch yourself over CDP
(`chrome --remote-debugging-port=9222 --user-data-dir=…`): Playwright never
launches one (see [Cloudflare](#cloudflare)).

The important seam is the [`Source` protocol](src/ricercar/sources/base.py) — the one interface a tracker adapter must satisfy. The pipeline, storage, deduplication, notifications and scheduling are all site-agnostic and shared by every source.

### The run flow

- On `--once`, the pipeline runs each configured source once: take the mandatory tasks plus a random sample of the rest, search each, skip what the database already knows (by URL *and* content MD5), download what is new, and stop at the daily quota.
- Failures are *graded* rather than retried blindly: a timeout is retried with exponential back-off, an expired session triggers a re-login, and a page whose selectors no longer match is **saved** (HTML, screenshot, trace) and the task skipped.
- An interrupt (`Ctrl-C`, `SIGTERM`) finishes the torrent in hand, saves state, and stops — the next run resumes where it stopped. A second interrupt leaves the schedule.

### The library layout

```
src/ricercar/
├── __main__.py     # `python -m ricercar`
├── cli.py          # the rich CLI + APScheduler repetition
├── pipeline.py     # one run: expands tasks, drives sources, grades failures
├── config.py       # layered, validated settings (YAML + env)
├── browser/        # CDP attach/close, manual-login waiting
├── sources/
│   ├── base.py    # the Source protocol + the failure taxonomy
│   └── rutracker/ # the one adapter: session, search, topic, selectors
├── parser/         # .torrent parsing → TorrentMetadata
├── repository/     # SQLite storage: db.py, migrations, legacy import (migrate.py)
├── history.py      # per-task yield over time, staleness
├── state.py        # resume state: known uploaders, completed tasks
├── diagnostics.py  # failure evidence: HTML, screenshot, trace
├── notify/         # apprise notifications (email, Discord, Telegram, Slack, …)
└── testing/        # FixtureRouter for offline development/tests
```

## Configuration

Configuration is a layered set of YAML files, resolved by pydantic-settings, with precedence (highest first):

1. `--config FILE` (repeatable; later files win)
2. `config/config.local.yml` (secrets — gitignored)
3. `config/config.yml` (version-controlled)
4. `RICERCAR__*` environment variables (YAML outranks them)
5. `.env` in the project root

Everything there is to set, with its default, is documented in [`config/config.example.yml`](config/config.example.yml). The notable pieces:

- **`browser`** — timeouts and where downloads land. How a browser is *obtained* is not configurable: you launch it yourself.
- **`sources.<name>`** — per-tracker settings: the CDP `connect_url`, login credentials, the `categories` map (`section name → id`), the search `tasks`, the daily `quota`, and an optional `selectors_file` to override the tracker's URLs/selectors without a code change.
- **`run`** — resume behaviour, where state/history files live, and what counts as a "stale" task.
- **`retry`** — exponential back-off for transient failures, with a separate, longer ceiling for "site under maintenance".
- **`diagnostics`** — where failure evidence goes, and whether traces are captured.
- **`schedule`** — the repeat interval and whether a run starts immediately.
- **`notify`** — the `notify.urls` list (apprise-supported schemes: `mailto://`, `discord://`, `telegram://`, `slack://`, `json://`, …). No URL means notifications are off, not an error.

## Requirements

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) (the project uses `uv` for dependency management)
- [just](https://github.com/casey/just) — for the day-to-day commands
- A Chromium-based browser installed (Chrome, Edge, Brave — any will do) **that you launch yourself**; Playwright's own browsers are never used and `playwright install` is not needed.
- Node.js ≥ 22, only for `commitlint` (the commit-message hook); the tool itself does not need Node.

## Installation

```sh
just setup          # uv sync + npm ci + git hooks (also `just`)
```

Or by hand:

```sh
uv sync --group dev
npm ci
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

## Cloudflare

The tracker this was written for is behind Cloudflare, and the site answers **only** a browser a human launched. So before a run you start a browser on its own profile and log in once:

```sh
# a profile of its own — Chrome ≥136 ignores --remote-debugging-port on the default profile
just browser                 # chrome --remote-debugging-port=9222 --user-data-dir=data/browser_profiles/cdp-chrome
```

Leave that window running and signed in; Ricercar attaches to it via CDP, reuses its session (cookies and `cf_clearance` persist in the profile), and — with `browser.close_on_exit: true` (the default) — closes it gracefully when done.

## Configuration for your setup

Copy `config/config.example.yml` to `config/config.yml` and:

- set the rutracker `connect_url` (default `http://localhost:9222`);
- put your credentials in `config/config.local.yml`:
  ```yaml
  sources:
    rutracker:
      login:
        username: your_username
        password: your_password
  ```
- adjust the `tasks` to the searches you want monitored, and the `quota` to the daily torrent limit;
- add notification URLs to `notify.urls`.

Check the resolved config and notifications with:

```sh
just config        # print the resolved configuration
just notify-test  # send a test notification to the configured URLs
```

## Running

```sh
just run         # run on the schedule (Ctrl-C once stops the current run, twice leaves)
just run-once   # one cycle, then exit
just run-once --limit 5   # store at most 5 torrents this run
just run-once --source rutracker   # only this source
```

The suite also works offline, against local HTML/torrent fixtures — no network, no login, no quota spent:

```sh
just offline           # one cycle against tests/fixtures
just offline --limit 1 # as above, storing at most one torrent
```

Review what each task has yielded, and which look dead:

```sh
just tasks           # worst kinds of tasks first
just tasks --stale  # only the ones worth removing
just tasks --json   # full data, machine-readable
just tasks --prune # drop history of tasks no longer in the config
```

Import a legacy database (see the `migrate` recipe or `src/ricercar/repository/migrate.py`):

```sh
just migrate                          # in-place upgrade of the configured database.path
just migrate path/to/old.db          # …or import one from somewhere else
```

Follow today's log:

```sh
just logs
```

## Development

```sh
just lint         # ruff lint
just format-check # ruff format --check
just typecheck   # mypy strict
just test        # the full pytest suite
just test-cov   # the suite with terminal coverage
just coverage   # the suite with coverage.xml for CI
```

Everything the CI workflow runs, in one command:

```sh
just ci          # lint + format-check + typecheck + test
```

## Adding a source (a new tracker)

The pipeline never sees a tracker's HTML: it drives a [`Source`](src/ricercar/sources/base.py). To add a tracker:

1. **Create a package** `src/ricercar/sources/<name>/` with a `Source` implementation satisfying the protocol:
   - `name` — the registry key (also the config section name)
   - `host` — the site's hostname (used to attribute stored torrents to this source for the daily quota)
   - `ensure_logged_in(page, …)` — make sure the attached browser holds a session for this tracker
   - `search(page, task, max_pages=…)` — an async generator yielding result pages
   - `fetch_torrent(page, hit, out_dir)` — download the `.torrent` behind one `SearchHit`
   - `diagnose(html)` — pure HTML-in, verdict-out: what is wrong with a page, so failures are explained
   - `expand(task)` — map a configured task onto one or more concrete searches
   - `fixture_routes()` — the URL-pattern → fixture-path map for offline mode
2. **Register it** in `SOURCES` in `src/ricercar/sources/__init__.py`.
3. **Add a `sources.<name>` config section** — that is what turns the source on.
4. **Add tests** — the existing suite works without a real browser by routing pages through `tests/fixtures/`, so your source gets the same treatment (capture its pages from the browser you attach to, per the "Recording fixtures" notes in [`src/ricercar/testing/__init__.py`](src/ricercar/testing/__init__.py)).

Nothing else in the project changes.

## CI

The CI (`.github/workflows/ci.yml`) runs on push to `main` and on pull requests:

- **`commits`** — enforces Conventional Commits on the pushed commits.
- **`quality`** — ruff lint, format check, and mypy (strict).
- **`tests`** — the pytest suite across Ubuntu, Windows and macOS × Python 3.12/3.13/3.14, each measuring coverage, uploading it to Codecov (per-OS/Python flags) and keeping the XML report as a build artifact. The three tests that drive a real browser skip themselves when none is attached, so CI needs no browser and makes no network calls.

## License

MIT