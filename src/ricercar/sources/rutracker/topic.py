"""Topic pages: availability, and capturing the ``.torrent`` download.

``page.expect_download()`` means no filesystem watcher is needed — the download
event itself tells us when the file is complete.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import Page

from ricercar.browser.navigation import goto
from ricercar.log import get_logger
from ricercar.models import TopicInfo
from ricercar.sources.base import SelectorsBrokenError, TorrentUnavailableError
from ricercar.sources.rutracker.diagnose import diagnose_topic
from ricercar.sources.rutracker.selectors import DEFAULT_SELECTORS, Selectors

DOWNLOAD_TIMEOUT = 60_000
"""Milliseconds to wait for the download event after clicking the link."""


def parse_topic(html: str, url: str, selectors: Selectors = DEFAULT_SELECTORS) -> TopicInfo:
    """Read a topic page: its title, and whether the torrent can be downloaded."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    return TopicInfo(
        url=url,
        title=title,
        downloadable=soup.select_one(selectors.download_link) is not None,
    )


async def open_topic(
    page: Page,
    url: str,
    *,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> TopicInfo:
    """Navigate to a topic page and read its information.

    Raises:
        SelectorsBrokenError: the page is not a topic page at all (a challenge, a
            login form, or a layout the selectors no longer match).
    """
    await goto(page, url)
    html = await page.content()
    issues = diagnose_topic(html, selectors)
    if issues:
        raise SelectorsBrokenError(issues)
    return parse_topic(html, page.url, selectors)


async def download(
    page: Page,
    out_dir: Path,
    *,
    fallback_name: str = "torrent",
    selectors: Selectors = DEFAULT_SELECTORS,
) -> Path:
    """Capture the ``.torrent`` of the topic page currently open.

    Raises:
        TorrentUnavailableError: there is no download link on the page.
    """
    link = page.locator(selectors.download_link).first
    if await link.count() == 0:
        msg = f"No download link on {page.url}"
        raise TorrentUnavailableError(msg)

    out_dir.mkdir(parents=True, exist_ok=True)
    async with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as download_info:
        await link.click()
    download = await download_info.value

    dest = out_dir / (download.suggested_filename or f"{fallback_name}.torrent")
    await download.save_as(dest)
    get_logger().info("Downloaded {} ({} bytes)", dest.name, dest.stat().st_size)
    return dest


async def fetch_torrent(
    page: Page,
    url: str,
    out_dir: Path,
    *,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> tuple[TopicInfo, Path]:
    """Open a topic and capture its ``.torrent``.

    Raises:
        TorrentUnavailableError: the torrent is not available for download.
        PlaywrightTimeout: the download never started.
    """
    info = await open_topic(page, url, selectors=selectors)
    if not info.downloadable:
        msg = f"Torrent is not available on {url}"
        raise TorrentUnavailableError(msg)

    path = await download(page, out_dir, selectors=selectors)
    return info, path


__all__ = [
    "DOWNLOAD_TIMEOUT",
    "download",
    "fetch_torrent",
    "open_topic",
    "parse_topic",
]
