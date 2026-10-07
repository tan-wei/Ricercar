"""The trackers this project can monitor.

A tracker lives in its own package below this one, exposes one ``Source``
implementation, and is listed here. Everything outside a source package talks to
the protocol (:mod:`ricercar.sources.base`), so adding a second tracker is an
additive change: no other module needs to learn about it, and nothing outside its
package knows what its HTML looks like.
"""

from __future__ import annotations

from ricercar.config import Settings, get_settings
from ricercar.sources.base import Source, SourceFactory, UnknownSourceError
from ricercar.sources.rutracker import RutrackerSource

SOURCES: dict[str, SourceFactory] = {RutrackerSource.name: RutrackerSource}
"""Every implemented tracker, by name."""


def get_source(name: str, cfg: Settings | None = None) -> Source:
    """Build the source registered as *name*, from its configuration section.

    Raises:
        UnknownSourceError: nothing is registered under *name*, or the config has
            no ``sources.<name>`` section.
    """
    cfg = cfg or get_settings()
    try:
        factory = SOURCES[name]
    except KeyError:
        known = ", ".join(sorted(SOURCES)) or "none"
        msg = f"Unknown source {name!r} — implemented sources: {known}"
        raise UnknownSourceError(msg) from None

    try:
        source_settings = cfg.sources[name]
    except KeyError:
        msg = f"Source {name!r} has no configuration — add a `sources: {name}:` section."
        raise UnknownSourceError(msg) from None

    return factory(source_settings)


def enabled_sources(cfg: Settings | None = None) -> list[Source]:
    """Build every source that is configured, in the order the config lists them.

    Configuration *is* the switch: a tracker runs when it has a ``sources.<name>``
    section. A configured name with no implementation is a typo, and raises.

    Raises:
        UnknownSourceError: a configured source has no implementation.
    """
    cfg = cfg or get_settings()
    return [get_source(name, cfg) for name in cfg.sources]


__all__ = ["SOURCES", "Source", "UnknownSourceError", "enabled_sources", "get_source"]
