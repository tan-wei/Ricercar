"""Category-list scraping for rutracker.

Extracts the full forum-section dropdown (``<select id="fs-main">``) from the search
page and returns it as ``{id: russian_name}``.  This is the baseline the pipeline uses
to detect when the site's section structure has changed (new categories, renames,
deletions).
"""

from __future__ import annotations

from playwright.async_api import Page

from ricercar.sources.rutracker.selectors import DEFAULT_SELECTORS, Selectors


async def fetch_categories(
    page: Page,
    *,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> dict[int, str]:
    """Navigate to the search page and extract the full category list.

    Returns ``{forum_id: russian_name}`` for every section in the dropdown,
    including sub-forums (indented with ``|-`` in the name).
    """
    url = selectors.search_url("", None)
    await page.goto(url, wait_until="domcontentloaded")

    # Wait for the category dropdown to be present.
    await page.wait_for_selector(selectors.category_list, timeout=15_000)

    categories: dict[int, str] = {}
    options = await page.query_selector_all(selectors.category_list)
    for option in options:
        value = await option.get_attribute("value")
        text = await option.inner_text()
        if value and value.isdigit():
            vid = int(value)
            name = (text or "").strip()
            if vid > 0 and name:
                categories[vid] = name

    return categories


async def fetch_categories_from_html(
    html: str,
    *,
    selectors: Selectors = DEFAULT_SELECTORS,
) -> dict[int, str]:
    """Extract categories from a raw HTML page (used in tests / offline mode).

    Parses the ``#fs-main`` select and returns ``{id: russian_name}``.
    """
    from playwright.async_api import async_playwright  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html, wait_until="domcontentloaded")
        try:
            return await fetch_categories(page, selectors=selectors)
        finally:
            await browser.close()


__all__ = ["fetch_categories", "fetch_categories_from_html"]
