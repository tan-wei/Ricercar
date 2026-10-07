"""
Testing utilities for offline Playwright development.

Provides a ``FixtureRouter`` that intercepts page requests and serves
locally saved HTML fixtures instead of hitting the real website.

Recording fixtures
------------------
The tracker rejects non-browser clients outright (Cloudflare — see the README),
so a page can only be captured from the browser you attached to::

    html = await page.content()
    Path("tests/fixtures/html/search/bach.html").write_text(html, encoding="utf-8")

Using fixtures
--------------
In tests / offline development::

    from ricercar.testing import FixtureRouter

    router = FixtureRouter()
    router.register(
        "https://example.org/forum/search.php*",
        "tests/fixtures/html/search/bach.html",
    )

    await router.apply(page)
    await page.goto("https://example.org/forum/search.php?q=bach")
    # No real network call — serves bach.html
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

FIXTURE_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "tests" / "fixtures"
"""Absolute path to the ``tests/fixtures/`` directory."""


class FixtureRouter:
    """Maps URL patterns to local HTML / torrent fixture files.

    Designed to be used with Playwright's ``page.route()`` API::

        router = FixtureRouter()
        router.register("**/forum/viewtopic.php*", "html/topic/sample.html")
        router.register("**/dl.php*", "torrents/sample.torrent")
        await router.apply(page)          # registers all routes at once

    A ``.torrent`` fixture is served as a download — ``Content-Disposition:
    attachment`` and the bit-torrent MIME type — because that is what the browser
    would have received from the tracker, and it is what makes
    ``page.expect_download()`` fire offline.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self._routes: list[tuple[str, Path, str | None]] = []
        self._root = Path(root) if root else FIXTURE_ROOT

    # ── Registration ───────────────────────────────────────────────────

    def register(
        self,
        url_pattern: str,
        fixture_rel_path: str,
        content_type: str | None = None,
    ) -> None:
        """Register a URL pattern to be served from a local fixture file.

        Args:
            url_pattern: Playwright glob pattern for the URL to intercept.
            fixture_rel_path: Relative path under the fixture root.
            content_type: MIME type override (e.g. ``application/json``).
        """
        fixture_path = self._root / fixture_rel_path
        if not fixture_path.exists():
            msg = (
                f"Fixture not found: {fixture_path}\n"
                f"Hint: capture it from the browser you attached to "
                f"(see 'Recording fixtures' in this module's docstring)."
            )
            raise FileNotFoundError(msg)
        self._routes.append((url_pattern, fixture_path, content_type))

    # ── Playwright integration ─────────────────────────────────────────

    async def apply(self, page: Any) -> None:
        """Register all previously added routes onto *page*."""
        for url_pattern, fixture_path, content_type in self._routes:
            body = fixture_path.read_bytes()
            resolved_type = content_type or _guess_content_type(fixture_path)
            headers = _download_headers(fixture_path) if fixture_path.suffix == ".torrent" else {}
            await page.route(url_pattern, _fulfill_handler(body, resolved_type, headers))

    def patterns(self) -> dict[str, Path]:
        """Return {url_pattern: fixture_path} for inspection purposes."""
        return {p: fp for p, fp, _ in self._routes}


# ── Helpers ──────────────────────────────────────────────────────────────


def _download_headers(path: Path) -> dict[str, str]:
    """Make the browser treat a fixture as a download, as the tracker would."""
    return {
        "Content-Disposition": f'attachment; filename="{path.name}"',
        "Content-Type": "application/x-bittorrent",
    }


def _fulfill_handler(
    body: bytes,
    content_type: str,
    headers: dict[str, str] | None = None,
) -> Any:
    """Build a Playwright route handler that serves *body* as *content_type*.

    Playwright always invokes route handlers as ``handler(route, request)``,
    so the handler must accept exactly two positional parameters.
    """

    async def handler(route: Any, _request: Any) -> None:
        await route.fulfill(body=body, content_type=content_type, headers=headers or {})

    return handler


def _guess_content_type(path: Path) -> str:
    """Guess MIME type from file extension."""
    suffix = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".torrent": "application/x-bittorrent",
        ".json": "application/json",
        ".txt": "text/plain; charset=utf-8",
    }.get(suffix, "application/octet-stream")
