#!/usr/bin/env python3
"""Probe one source directly against a live tracker.

The normal path is ``python -m ricercar --once`` (see "Running" in ``README.md``);
this script exists to drive a *single* source by hand — session, search, uploader
filter, download, in two tabs — when one interaction needs looking at in isolation.
It deliberately shares no code with the pipeline, so a bug in one cannot hide in the
other.

Start your browser first, then attach (see the "Cloudflare" section of README.md)::

    chrome.exe --remote-debugging-port=9222 --user-data-dir=<profile dir>

    uv run python scripts/smoke_rutracker.py --query Bach --limit 1
    uv run python scripts/smoke_rutracker.py --query Bach --pages 3 --no-download
    uv run python scripts/smoke_rutracker.py --query Bach --author Shmuma --limit 2
    uv run python scripts/smoke_rutracker.py --query Bach --category 794 --limit 2
    uv run python scripts/smoke_rutracker.py --query Bach --all-sections --no-download
    uv run python scripts/smoke_rutracker.py --source rutracker --query Bach

A search always names a section (the site's results URL has an ``f=<id>``): ``--category``
picks one, and without it the task means "every configured section" — 28 searches for the
real config, so this probe walks the first one unless ``--all-sections`` asks for the rest.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout
from rich.progress import Progress

from ricercar.browser import MANUAL_LOGIN_TIMEOUT, BrowserSession, managed_browser
from ricercar.config import SearchTask, Settings, get_settings
from ricercar.log import configure_logging, get_logger
from ricercar.models import SearchHit
from ricercar.parser import TorrentError, parse_file
from ricercar.progress import new_progress
from ricercar.repository import TorrentRepository
from ricercar.sources import SOURCES, Source, UnknownSourceError, get_source


async def _handle_hit(
    hit: SearchHit,
    page: Page,
    out_dir: Path,
    repo: TorrentRepository,
    source: Source,
) -> int:
    """Download, parse and store one hit. Returns 1 when a torrent was stored."""
    log = get_logger()

    try:
        info, path = await source.fetch_torrent(page, hit, out_dir)
    except (RuntimeError, PlaywrightTimeout) as exc:
        log.warning("Skipping {}: {}", hit.url, exc)
        return 0

    try:
        meta = parse_file(path)
    except TorrentError as exc:
        log.error("Unreadable torrent from {}: {}", hit.url, exc)
        return 0

    log.info("  topic: {} (downloadable={})", info.title[:70], info.downloadable)
    for issue in meta.issues:
        log.warning("  torrent issue: {}", issue)

    if not meta.url:
        # Not every torrent carries a `comment`; fall back to where we got it.
        meta = replace(meta, url=hit.url)

    stored = repo.add(meta, path.read_bytes())
    log.info(
        "  parsed: name={!r} size={} files={} md5={}",
        meta.name,
        meta.size,
        meta.file_count,
        meta.md5,
    )
    log.info("  stored: {}", "yes" if stored else "no (already in DB)")
    return 1


async def _scrape(
    args: argparse.Namespace,
    source: Source,
    results_page: Page,
    topic_page: Page,
    *,
    out_dir: Path,
    repo: TorrentRepository,
    progress: Progress,
) -> None:
    log = get_logger()

    await source.ensure_logged_in(results_page, timeout=args.login_timeout)

    task = SearchTask(text=args.query, author=args.author, category=args.category)
    sections = {section: name for name, section in source.settings.categories.items()}
    searches = source.expand(task)
    if len(searches) > 1:
        # No --category: the task means "every configured section", which is 28 searches
        # by hand for the real config, so probe the first one unless asked for all.
        if not args.all_sections:
            searches = searches[:1]
        first = searches[0].category
        log.info(
            "No --category given — {} section(s) configured; probing {} ({}){}",
            len(sections),
            sections.get(first, "?"),
            first,
            "" if args.all_sections else "; --all-sections walks them all",
        )

    downloads = progress.add_task(
        "Downloading",
        total=args.limit,
        visible=not args.no_download,
    )

    downloaded = 0
    skipped = 0
    for search in searches:
        pages = progress.add_task(f"Searching {args.query!r}", total=args.pages)
        description = sections.get(search.category, "?")
        try:
            async for hits in source.search(results_page, search, max_pages=args.pages):
                progress.advance(pages)
                for index, hit in enumerate(hits[:5], start=1):
                    log.info(
                        "  {}. [{}] {} | {} | {}",
                        index,
                        hit.topic_id,
                        hit.title[:60],
                        hit.size,
                        hit.author,
                    )

                if args.no_download:
                    continue

                for hit in hits:
                    if downloaded >= args.limit:
                        break
                    progress.update(downloads, description=f"Downloading [{hit.topic_id}]")
                    stored = await _handle_hit(hit, topic_page, out_dir, repo, source)
                    downloaded += stored
                    skipped += 1 - stored
                    progress.update(downloads, description="Downloading", advance=stored)
                if downloaded >= args.limit:
                    log.info("Reached --limit {}; stopping pagination", args.limit)
                    break
        finally:
            progress.remove_task(pages)

        log.info("Done with {} ({}): {} stored so far", description, search.category, downloaded)
        if downloaded >= args.limit and not args.no_download:
            log.info("Reached --limit {}; stopping", args.limit)
            break

    log.info("Done — downloaded={}, skipped={}, stored={}", downloaded, skipped, repo.count())


async def _scrape_in_tabs(
    args: argparse.Namespace,
    source: Source,
    session: BrowserSession,
    *,
    out_dir: Path,
    repo: TorrentRepository,
    progress: Progress,
) -> None:
    """Drive one results tab and one download tab, always closing both."""
    results_page = await session.context.new_page()
    topic_page = await session.context.new_page()
    try:
        await _scrape(
            args,
            source,
            results_page,
            topic_page,
            out_dir=out_dir,
            repo=repo,
            progress=progress,
        )
    finally:
        await results_page.close()
        await topic_page.close()


def _pick_source(args: argparse.Namespace, cfg: Settings) -> Source:
    """Resolve the source to drive: the one asked for, else the first configured."""
    try:
        return get_source(args.source or next(iter(cfg.sources), ""), cfg)
    except UnknownSourceError as exc:
        raise SystemExit(
            f"{exc}\nConfigured: {', '.join(cfg.sources) or 'none'}\n"
            f"Implemented: {', '.join(sorted(SOURCES))}"
        ) from exc


async def run(args: argparse.Namespace) -> int:
    cfg = get_settings()
    log = get_logger()
    source = _pick_source(args, cfg)
    log.info("Source: {}", source.name)

    repo = TorrentRepository(cfg.db_path)
    with repo, new_progress() as progress:
        try:
            async with managed_browser(source, cfg) as session:
                await _scrape_in_tabs(
                    args,
                    source,
                    session,
                    out_dir=cfg.download_dir,
                    repo=repo,
                    progress=progress,
                )
        except RuntimeError as exc:
            # Expected failures: no browser to attach to, or nobody logged in in time.
            log.error("{}: {}", type(exc).__name__, exc)
            return 1

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        default=None,
        help="which configured tracker to drive (default: the first one)",
    )
    parser.add_argument("--query", default="Bach", help="search text (default: Bach)")
    parser.add_argument(
        "--category",
        type=int,
        default=None,
        help="rutracker forum id (f=…) — without it the task means every configured section",
    )
    parser.add_argument(
        "--all-sections",
        action="store_true",
        help="with no --category: walk every configured section instead of the first one",
    )
    parser.add_argument("--author", default=None, help="restrict results to one uploader")
    parser.add_argument("--limit", type=int, default=1, help="how many torrents to store")
    parser.add_argument("--pages", type=int, default=1, help="how many result pages to walk")
    parser.add_argument(
        "--no-download", action="store_true", help="only check the session and search"
    )
    parser.add_argument(
        "--login-timeout",
        type=float,
        default=MANUAL_LOGIN_TIMEOUT,
        help="seconds to wait for a manual login (default: %(default)s)",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    configure_logging()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
