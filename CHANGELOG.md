# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- **An expired session is now signed in automatically.** `ensure_logged_in` reuses the
  attached browser's session, and when that session is gone it fills the tracker's login
  form with `login.username`/`login.password` and submits it — in the browser you already
  launched, which is the one client the site does not serve a Cloudflare interstitial to.
  Verified against the live tracker: cookies dropped over CDP, run started, signed in
  again in 3.6 s. Waiting for a human is still the fallback, and now logs which of the
  four reasons (no form, interstitial, captcha, credentials refused) made it necessary.
- **The browser is closed when the program exits.** `browser.close_on_exit` (default
  `true`) closes the browser this program attached to — a CDP `Browser.close` first, then
  a terminate signal to the process, so the profile is flushed rather than lost. The
  cookies live in that profile, so the session survives the shutdown: relaunching the same
  `--user-data-dir` comes back signed in.
- **Coverage is uploaded to Codecov by CI.** Each of the six test cells — `ubuntu-latest`
  and `windows-latest` × Python 3.12, 3.13, 3.14 — runs `just coverage` and uploads
  `coverage.xml` under a flag naming the cell, then keeps it as a build artifact: the same
  shape TagMuse uses, so the badge is the merge of all six rather than one platform's view.
  `fail_ci_if_error: false` is set, because a Codecov hiccup is not a failing suite.

### Changed

- **`just migrate` imports the database where the configuration says it is, in place.** Its
  argument is optional now, and `--target` and `--replace` are gone: the source is
  `database.path` — the file a run uses — and the *same* file, with the same name, is what
  comes out on the current schema. Nothing refers to a `legacy/` directory any more. An
  existing database is copied aside first (`<name>.backup-<timestamp>`, a full copy
  including its write-ahead log) instead of being overwritten only on request, and a
  database that has already been imported reports that there is nothing to import rather
  than failing — so the command can be run twice. Naming a file still imports one kept
  elsewhere, into the configured database, and the copy is made with a progress bar.
- The `justfile` works on Linux and macOS as well as Windows. The recipe that launches the
  browser now has a `[unix]` and a `[windows]` variant (detaching a browser differs per
  platform), it defaults to the platform's usual binary, and `just logs` asks `just` for the
  date instead of shelling out to `date`. Every recipe is `sh`, which is what `just` itself
  defaults to — Git for Windows included.

## 0.1.0 — 2026-10-07
The rewrite of the Selenium-era script in `legacy/`, as a maintainable project.

### Added

- **Browser access through a browser you launch yourself.** Playwright only ever *attaches*
  over CDP (`sources.<name>.connect_url`), reusing the existing context so the signed-in
  session carries over; nothing is launched and nothing is closed on exit. Measured against
  the live tracker: Playwright's bundled Firefox (headless *and* headed), Edge via
  `channel="msedge"` and `requests` replaying exported cookies are all refused; a browser a
  human started is not (see README, "Cloudflare").
- **A multi-tracker architecture.** One `Source` protocol (`sources/base.py`) plus an
  adapter per tracker (`sources/rutracker/`), a registry, and one configuration section per
  tracker — a second tracker is another package and another section, with no change to the
  pipeline, the storage or the notifications.
- **The run pipeline** (`pipeline.py`): mandatory tasks plus a random sample, paginated
  search, per-tracker daily quota counted in the database, deduplication by URL before
  download and by MD5 after it, progress bars per source, and a notification at start and
  at the end (including how many uploaders were seen for the first time).
- **The legacy task vocabulary, complete**: a task is keywords, an uploader, an uploader in
  one section, or "every configured section" (`category: "*"`, the default). The last one
  expands through `Source.expand()`, so one configured task is one search per section —
  deduped, resumable and counted as searches by the progress bars and the quota. The
  migrated list is 5,969 tasks over 28 sections: 3,388 searches a run.
- **A task's long-term yield, and a pointer at the ones that stopped yielding**
  (`history.py`): how often each configured task ran, how many result rows came back, when
  it last found anything — `run.task_history_file`, written once per run and deliberately
  *not* pruned, since the question it answers spans months. A task quiet for
  `run.stale_task_days` (60) over at least `run.stale_task_runs` (5) runs is named at the
  end of a run and counted in the notification; nothing is deleted for you. `ricercar tasks`
  prints the whole list, worst first, with `--stale`, `--all`, `--json` and `--prune`.
- **Retries graded by failure type** (`retry.py`): a timeout backs off further than a
  browser hiccup, an expired session logs in again, and a page whose selectors no longer
  match is not retried at all. `sleep` is injectable, so the backoff is asserted without
  waiting for it.
- **Failure evidence** (`diagnostics.py`): every failure gets its own directory with
  `page.html`, `page.png`, the Playwright trace of the step that failed and a
  `failure.json` naming the reason — a selector that stopped matching cannot be fixed
  without seeing the page. Traces are recorded per results page and discarded when the page
  was fine, so a healthy run does not pile them up.
- **Resumable runs** (`state.py`): known uploaders, the tasks finished today and the last
  run, in an atomically written JSON file. An interrupted run picks up where it stopped; a
  task cut short is not recorded as done.
- **Graceful interrupts**: SIGINT/SIGTERM finish the torrent in hand, save the state, say
  so in the notification, and stop — exit code `130` (a legacy database or a browser that
  is not running: `1`).
- **Scheduled runs**: `ricercar` with no `--once` repeats on `schedule.interval_minutes`
  (60 by default), optionally straight away (`run_on_start`). Ctrl-C twice: the first stops
  the current cycle, the second leaves the schedule.
- **Offline mode** (`--offline`): the whole pipeline against saved pages, with a catch-all
  route refusing every request the fixtures do not answer, so "offline" is a fact rather
  than a hope — including `.torrent` fixtures served as downloads.
- **Notifications through apprise** (`notify/`), so email, Discord, Telegram, Slack and
  anything else apprise speaks works from one URL list.
- **Legacy database import** (`repository/migrate.py`): read-only `ATTACH` plus
  `INSERT ... SELECT` (the blobs never pass through Python), fingerprinted against the
  source afterwards, staged next to the target and swapped in only once it verifies — which
  is also what makes an in-place import of a 600 MiB database safe.
- **Forward migrations** (`repository/migrations.py`): a database this project wrote
  earlier is upgraded in place, one version at a time, each step committing together with
  its `user_version` stamp. Version 3 indexes `url_table(add_date)`; the daily counters
  query it as a range (5.9 ms → 0.07 ms for the per-tracker count on a 25k-row database).
- **A test suite** of 157 tests — 154 of them need neither a browser nor the network — plus
  CI (lint, format, types, tests) on Python 3.12, 3.13 and 3.14.
- **`config/config.example.yml`**: every setting there is, with its default.
- Debugging aids: `show-config`, `notify-test`, `tasks`, `--dry-run`, `--limit`,
  `scripts/smoke_rutracker.py` and `--config FILE` (repeatable, later files win).

### Changed

- Failures are no longer retried identically: legacy retried everything three times with
  no delay, which turns a rate limit into three immediate refusals.
- The daily quota is per tracker, counted from the database (by the host in the stored
  URL), so it survives a restart and does not need a `source` column.
- Uploaders are read from the result row's uploader cell; legacy's
  `class_="med ts-text"` also matched the title link, so its "authors" set contained titles.
- Configuration is layered and validated (`pydantic-settings`): `config.yml`,
  `config.local.yml` for secrets, `--config` files, `RICERCAR__*` variables.
- Progress and logs share one console (rich), replacing the `tqdm` habit.
- The status file keeps only *today's* finished searches. It used to accumulate for ever,
  which a task list that expands into thousands of searches would have made painful; the
  resume state is now per search too, so an interrupted "every section" task redoes only
  the sections it never reached.

### Removed

- The `launch` browser mode. There is only one way to drive a browser, and it is the one
  that is not refused: attaching to yours.
- Playwright's bundled browsers, the `playwright install` step, the cookie-import script
  and the fixture-capture document — all of them existed only to prop up launching.
- The file observer (`watchdog`): `page.expect_download()` says when a download is done.
- Searching with no section at all. Every search names one; the legacy tool worked that way
  too, and a results URL without `f=<id>` was never what it wanted.
- The old `scraper/` package, replaced by the `Source` protocol and its adapters.
- Docker: see the note in `README.md` and `TODO.md` §3.9.

### Fixed

- A refused database no longer leaves an open connection behind: `TorrentRepository.open()`
  closes what it opened when the schema check raises, instead of relying on the garbage
  collector.
- Creating a fresh database is now one transaction with its version stamp, so a failure
  halfway cannot leave a file that looks like a legacy database.

### Notes

- Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). There is no
  `playwright install` step: the `playwright` package is used for its CDP client only.
- Coming from the legacy tool: put the old database at `database.path` and run `just migrate` — it keeps a copy and rebuilds that same file.
  A database this project wrote earlier upgrades itself the first time it is opened — from
  version 2 that means one index on `url_table(add_date)`, measured at 0.09 s on a 631 MiB
  file holding 25,448 torrents.
