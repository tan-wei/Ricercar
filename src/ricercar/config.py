"""
Configuration system — powered by pydantic-settings.

Supports layered config resolution:
  1. config.local.yml  (highest priority, gitignored — for secrets & local overrides)
  2. config.yml        (version-controlled defaults)
  3. Environment variables  (prefix RICERCAR__)
  4. .env file         (project root)

All values are validated & coerced to their declared types at load time.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# ── Paths ──────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""Path to the repository root (where config/ lives)."""

DATA_DIR = PROJECT_ROOT / "data"
"""Runtime data directory (downloaded torrents, temp files, DB)."""

DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "config.yml"
LOCAL_CONFIG_FILE = PROJECT_ROOT / "config" / "config.local.yml"

_extra_config_files: list[Path] = []
"""Additional YAML files registered via :func:`set_extra_config_files` (highest priority)."""


def _yaml_files() -> list[str]:
    """YAML config files in ascending priority order (later files override earlier ones)."""
    return [
        str(DEFAULT_CONFIG_FILE),
        str(LOCAL_CONFIG_FILE),
        *(str(path) for path in _extra_config_files),
    ]


# ── Enums ──────────────────────────────────────────────────────────────────


class LogLevel(enum.StrEnum):
    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# ── Sub-models ─────────────────────────────────────────────────────────────


class LoginConfig(BaseModel):
    """Credentials for one tracker account.

    A login cannot always be automated — for a trackers behind Cloudflare every
    ``login.php`` POST is blocked, so a human performs it in the browser (see
    ``README.md``). This is also what happens when a session expires mid-run, so
    the credentials are configured here as a pair.
    """

    username: str = ""
    """Also used programmatically, to verify that the current session belongs to
    the expected account."""
    password: str = ""
    """Not sent automatically (the login POST is blocked); present because a session
    expiry forces a manual login and the credentials belong together."""


class BrowserConfig(BaseModel):
    """How to drive a browser — settings shared by every source.

    How a browser is *obtained* is not configurable and not a per-source choice:
    every source is driven through a browser the user launched by hand (see
    :mod:`ricercar.browser`), because that is the one client a tracker's anti-bot
    layer does not treat as automation. What is shared here is how patient the
    driver should be, and where downloads land.
    """

    navigation_timeout: Annotated[int, Field(ge=5_000, le=300_000)] = 30_000
    """Timeout in ms for page navigations (Playwright default: 30s)."""
    action_timeout: Annotated[int, Field(ge=1_000, le=120_000)] = 15_000
    """Timeout in ms for individual actions (click, fill, etc.)."""
    downloads_dir: str = str(DATA_DIR / "torrents")
    """Where downloaded .torrent files land."""


class SearchTask(BaseModel):
    """A single search task — keywords, an uploader, and where to look.

    The vocabulary is the tracker's: ``author`` is its uploader field and ``category``
    its own section id. What a task leaves out it does not filter on, with one
    exception that is worth knowing: a task that names **no** category searches every
    section listed under ``categories`` (write it as ``category: "*"`` to say so
    explicitly). That is what the legacy tool did — it searched each configured
    section in turn — and it is why the sections are configured at all.

    So, the four shapes a task takes on rutracker:

    ``{author: "X"}``
        everything that uploader has posted, in every configured section;
    ``{text: "Bach"}``
        anything matching those keywords, in every configured section;
    ``{author: "X", category: 794}``
        that uploader inside one section (``category`` is ``f`` on the site);
    ``{category: "*"}``
        everything in every configured section — "anything I have not seen yet".

    ``text`` and ``author`` may also be combined with each other.
    """

    text: str = ""
    author: str | None = None
    category: int | Literal["*"] | None = None
    """A section id, ``"*"`` for every configured section, or ``None`` — which means
    the same as ``"*"``."""


class QuotaConfig(BaseModel):
    """Daily download quota."""

    limit_torrents_one_day: Annotated[int, Field(ge=0)] = 50
    """Maximum torrents to download per day."""


class SourceSettings(BaseModel):
    """Everything the application knows about one tracker.

    A source reads only what applies to it — a tracker without an account never
    touches ``login`` — and a tracker runs exactly when it has a section here, so
    the config file *is* the list of monitored trackers.
    """

    connect_url: str = "http://localhost:9222"
    """CDP endpoint of the browser this source is driven through — a browser *you*
    launched, e.g. via ``chrome.exe --remote-debugging-port=9222
    --user-data-dir=<dir>``. Two sources may point at the same browser or at
    different ones."""
    login: LoginConfig = Field(default_factory=LoginConfig)
    selectors_file: str | None = None
    """Optional YAML file overriding the source's built-in selectors, URLs and
    literal strings — lets a site redesign be patched without a code change."""

    categories: dict[str, Annotated[int, Field(gt=0)]] = Field(default_factory=dict)
    """The sections of this tracker worth monitoring, ``name → id``, in the order they
    should be searched in.

    A task that names no category searches all of them, so this list is what "every
    category" means — and what keeps a keyword search out of the sections nobody asked
    about. On rutracker the id is the ``f`` parameter (a forum), and the name is only
    for reading your own config and the logs."""
    tasks: list[SearchTask] = Field(default_factory=list)
    """Primary search tasks (sampled randomly each run)."""
    must_complete_tasks: list[SearchTask] = Field(default_factory=list)
    """Tasks that MUST be executed every run (not subject to random_choice)."""
    random_choice: Annotated[int, Field(ge=0)] = 3
    """How many tasks to randomly pick each run."""
    max_pages: Annotated[int, Field(ge=1)] = 50
    """Maximum pages to traverse per search."""
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    """How much may be taken from this tracker per day."""

    @model_validator(mode="after")
    def _categories_are_needed_by_some_task(self) -> SourceSettings:
        """Refuse a task that has to search "every category" when none are listed.

        Silent would be worse: the task would simply never match anything, and the
        run would look like it worked.
        """
        if self.categories:
            return self

        without = [
            task
            for task in (*self.tasks, *self.must_complete_tasks)
            if not isinstance(task.category, int)
        ]
        if without:
            msg = (
                "a task that does not name a category searches every section listed "
                "under `categories`, and none are configured — either list the sections "
                "(`categories: {Opera_Lossless: 794, …}`, see config/config.example.yml) "
                "or give the task a `category: <id>`"
            )
            raise ValueError(msg)
        return self


class RetryConfig(BaseModel):
    """Retry behaviour for flaky operations."""

    max_attempts: Annotated[int, Field(ge=0, le=20)] = 3
    base_delay: Annotated[float, Field(ge=0)] = 2.0
    max_delay: Annotated[float, Field(ge=0)] = 60.0
    backoff_factor: Annotated[float, Field(ge=1)] = 2.0
    """Multiplicative factor for exponential backoff."""


class LogConfig(BaseModel):
    """Loguru-based logging settings."""

    level: LogLevel = LogLevel.DEBUG
    format: str = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</> | "
        "<level>{level: <8}</> | "
        "<cyan>{name}</>:<cyan>{function}</>:<cyan>{line}</> - "
        "<level>{message}</>"
    )
    """Console log format (uses loguru colour tags)."""
    colorize: bool = True
    file_rotation: str = "1 day"
    """Log file rotation interval (e.g. '1 day', '100 MB')."""
    file_retention: str = "30 days"
    """How long to keep old log files."""
    json_output: bool = False
    """If true, emit JSON-structured logs (machine-parseable)."""


class DatabaseConfig(BaseModel):
    """SQLite database settings."""

    path: str = str(DATA_DIR / "torrents.db")


class RunConfig(BaseModel):
    """How a run behaves."""

    resume: bool = True
    """Skip the tasks already completed today (see :mod:`ricercar.state`)."""
    state_file: str = str(DATA_DIR / "state.json")
    """Where run state lives: known uploaders, completed tasks, last run."""
    task_history_file: str = str(DATA_DIR / "task_history.json")
    """Where each task's long-term yield lives (see :mod:`ricercar.history`)."""
    stale_task_days: Annotated[int, Field(ge=1)] = 60
    """A task that has found nothing for this many days is reported for removal. Only
    after ``stale_task_runs`` runs, so a fresh configuration is never nagged."""
    stale_task_runs: Annotated[int, Field(ge=1)] = 5
    """How many runs a task needs before its age is trusted enough to judge it."""


class DiagnosticsConfig(BaseModel):
    """What to keep when something fails mid-run."""

    failures_dir: str = str(DATA_DIR / "failures")
    """One directory per failure: the page, a screenshot, the issues, the trace."""
    save_traces: bool = True
    """Record Playwright traces per step and keep the trace of the step that failed
    (``playwright show-trace <dir>/trace.zip``). Costs time and disk, so it can be
    turned off."""


class ScheduleConfig(BaseModel):
    """Scheduled runs (the default when the CLI is not told to run once)."""

    interval_minutes: Annotated[int, Field(ge=1)] = 60
    """How often to run."""
    run_on_start: bool = True
    """Run immediately at startup instead of waiting for the first interval."""


class NotifyConfig(BaseModel):
    """Notification settings (powered by apprise).

    See https://github.com/caronc/apprise for supported URLs.
    """

    urls: list[str] = Field(default_factory=list)
    """One or more Apprise notification URLs (e.g. slack://, discord://, mailto://...)."""


# ── Root settings ──────────────────────────────────────────────────────────


class Settings(BaseSettings):
    """Root configuration object.

    Loaded from (highest priority first):
      - extra YAML files registered via :func:`set_extra_config_files`
      - config/config.local.yml  (secrets & local overrides, gitignored)
      - config/config.yml        (defaults, version-controlled)
      - environment variables with prefix ``RICERCAR__`` (e.g. RICERCAR__LOG__LEVEL)
      - .env file in project root
    """

    model_config = SettingsConfigDict(
        yaml_file_encoding="utf-8",
        env_prefix="RICERCAR__",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Sections ────────────────────────────────────────────────────────
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    sources: dict[str, SourceSettings] = Field(default_factory=dict)
    """One section per monitored tracker, keyed by the source's name."""
    run: RunConfig = Field(default_factory=RunConfig)
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    # ── Derived helpers ─────────────────────────────────────────────────

    @property
    def download_dir(self) -> Path:
        return Path(self.browser.downloads_dir)

    @property
    def db_path(self) -> Path:
        return Path(self.database.path)

    @field_validator("browser")
    @classmethod
    def _ensure_downloads_dir(cls, v: BrowserConfig) -> BrowserConfig:
        Path(v.downloads_dir).mkdir(parents=True, exist_ok=True)
        return v

    # ── Custom YAML source (for loading two YAML files) ─────────────────

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: highest first. pydantic-settings applies the tuple from
        # last to first, so earlier entries take precedence.
        return (
            # YAML files (config.local.yml overrides config.yml; --config overrides both)
            YamlConfigSettingsSource(
                settings_cls,
                yaml_file=_yaml_files(),
                yaml_file_encoding="utf-8",
                deep_merge=True,
            ),
            env_settings,
            dotenv_settings,
            init_settings,
            file_secret_settings,
        )


# ── Global singleton (lazy-loaded) ─────────────────────────────────────────

_settings: Settings | None = None


def set_extra_config_files(paths: Iterable[str | Path]) -> None:
    """Register additional YAML config files (highest priority) and reset the cache."""
    global _extra_config_files, _settings
    _extra_config_files = [Path(path) for path in paths]
    _settings = None


def get_settings() -> Settings:
    """Return the global settings singleton, loading it on first call."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Force-reload settings from disk (useful for testing / hot-reload)."""
    global _settings
    _settings = Settings()
    return _settings
