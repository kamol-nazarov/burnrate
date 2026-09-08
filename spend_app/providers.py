"""Provider registry: identity, ingest callables, and capability reports.

I5/I6 own adapter modules and keep their ``ingest()`` signatures. This module
imports those callables and is the only place a new adapter is listed so the
scheduler (and later CLI/health) can iterate without per-provider branches.
``aggregate.py`` still holds display catalogs until I7; a new adapter must not
need a new conditional there once I7 reads this registry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import os
from typing import Literal

from spend_app.adapters.anthropic_admin import ingest as ingest_anthropic_admin
from spend_app.adapters.antigravity_local import ingest as ingest_antigravity_local
from spend_app.adapters.claude_local import ingest as ingest_claude_local
from spend_app.adapters.codex_local import ingest as ingest_codex_local
from spend_app.adapters.cursor_admin import ingest as ingest_cursor_admin
from spend_app.adapters.cursor_csv import ingest as ingest_cursor_csv
from spend_app.adapters.cursor_local import ingest as ingest_cursor_local
from spend_app.adapters.cursor_usage import ingest as ingest_cursor_usage
from spend_app.adapters.grok_local import coverage_start as grok_coverage_start
from spend_app.adapters.grok_local import ingest as ingest_grok_local
from spend_app.adapters.opencode_local import ingest as ingest_opencode_local
from spend_app.adapters.openai_admin import ingest as ingest_openai_admin
from spend_app.adapters.traycer_local import ingest as ingest_traycer_local
from spend_app.adapters.zcode_local import ingest as ingest_zcode_local
from spend_app.config import Settings
from spend_app.pricing import PricingEngine

Capability = Literal["usage", "quota", "activity", "admin"]
Stability = Literal["official", "experimental"]
Exactness = Literal["exact", "derived", "partial", "unavailable"]
IngestKind = Literal["local", "admin", "manual"]
IngestFn = Callable[..., dict]
KwargsFn = Callable[..., dict]
SkipFn = Callable[[Settings], bool]

CAPABILITIES: frozenset[str] = frozenset({"usage", "quota", "activity", "admin"})
STABILITIES: frozenset[str] = frozenset({"official", "experimental"})
EXACTNESS: frozenset[str] = frozenset({"exact", "derived", "partial", "unavailable"})
CAPABILITY_NAMES: tuple[str, ...] = ("usage", "quota", "activity", "admin")


def _home() -> Path:
    return Path.home()


def default_codex_glob() -> str:
    return str(Path(os.getenv("CODEX_HOME") or _home() / ".codex").expanduser() / "sessions" / "**" / "*.jsonl")


def default_claude_glob() -> str:
    return str(Path(os.getenv("CLAUDE_CONFIG_DIR") or _home() / ".claude").expanduser() / "projects" / "**" / "*.jsonl")


def default_traycer_glob() -> str:
    return str(_home() / ".traycer" / "host" / "epic-state" / "**" / "chat" / "chat.db")


def default_cursor_glob() -> str:
    return str(_home() / ".cursor" / "projects" / "**" / "sdk-agent-store" / "*" / "index.db")


def default_opencode_database() -> Path:
    return Path(os.getenv("XDG_DATA_HOME") or _home() / ".local" / "share").expanduser() / "opencode" / "opencode.db"


def default_zcode_database() -> Path:
    return _home() / ".zcode" / "cli" / "db" / "db.sqlite"


def default_grok_log() -> Path:
    return Path(os.getenv("GROK_HOME") or _home() / ".grok").expanduser() / "logs" / "unified.jsonl"


def _base_kwargs(settings: Settings, pricing: PricingEngine) -> dict:
    return {"database_path": settings.database_path, "pricing": pricing}


def _codex_kwargs(settings: Settings, pricing: PricingEngine, **_: object) -> dict:
    return {**_base_kwargs(settings, pricing), "session_glob": default_codex_glob()}


def _claude_kwargs(settings: Settings, pricing: PricingEngine, **_: object) -> dict:
    return {**_base_kwargs(settings, pricing), "session_glob": default_claude_glob()}


def _traycer_kwargs(settings: Settings, pricing: PricingEngine, **_: object) -> dict:
    return {
        **_base_kwargs(settings, pricing),
        "database_glob": default_traycer_glob(),
        "grok_covered_from": grok_coverage_start(
            default_grok_log(), settings.database_path
        ),
    }


def _cursor_local_kwargs(settings: Settings, pricing: PricingEngine, **_: object) -> dict:
    return {**_base_kwargs(settings, pricing), "database_glob": default_cursor_glob()}


def _pricing_only_kwargs(settings: Settings, pricing: PricingEngine, **_: object) -> dict:
    return _base_kwargs(settings, pricing)


def _opencode_kwargs(settings: Settings, pricing: PricingEngine, **_: object) -> dict:
    return {**_base_kwargs(settings, pricing), "source_database": default_opencode_database()}


def _zcode_kwargs(settings: Settings, pricing: PricingEngine, **_: object) -> dict:
    return {**_base_kwargs(settings, pricing), "source_database": default_zcode_database()}


def _grok_kwargs(settings: Settings, pricing: PricingEngine, **_: object) -> dict:
    return {**_base_kwargs(settings, pricing), "log_path": default_grok_log()}


def _openai_admin_kwargs(
    settings: Settings, pricing: PricingEngine, *, start, end, **_: object
) -> dict:
    return {
        **_base_kwargs(settings, pricing),
        "admin_key": settings.openai_admin_key,
        "start": start,
        "end": end,
    }


def _anthropic_admin_kwargs(
    settings: Settings, pricing: PricingEngine, *, start, end, **_: object
) -> dict:
    return {
        **_base_kwargs(settings, pricing),
        "admin_key": settings.anthropic_admin_key,
        "start": start,
        "end": end,
    }


def _cursor_admin_kwargs(
    settings: Settings, pricing: PricingEngine, *, start, end, **_: object
) -> dict:
    return {
        **_base_kwargs(settings, pricing),
        "api_key": settings.cursor_api_key,
        "start": start,
        "end": end,
    }


def _cursor_csv_kwargs(settings: Settings, pricing: PricingEngine, **_: object) -> dict:
    return {**_base_kwargs(settings, pricing), "import_path": settings.cursor_import_path}


def _skip_without_cursor_key(settings: Settings) -> bool:
    return not settings.cursor_api_key


def _skip_cursor_usage_when_admin_enabled(settings: Settings) -> bool:
    return bool(settings.cursor_api_key)


@dataclass(frozen=True)
class ConnectionSpec:
    name: str
    argument: str
    default: Callable
    suffix: str = ""
    shape: str = "jsonl"
    home_name: str = ""
    child: str = ""
    table: str = ""
    columns: tuple = ()
    credential_env: str = ""
    credential_help: str = ""
    location_env: str = ""
    manual_only: bool = False


# Setup metadata extends the same registry used by dispatch.
CONNECTION_SPECS = {
    "codex_local": ConnectionSpec("Codex", "session_glob", default_codex_glob, "**/*.jsonl", home_name=".codex", child="", location_env="CODEX_HOME"),
    "claude_local": ConnectionSpec("Claude Code", "session_glob", default_claude_glob, "**/*.jsonl", home_name=".claude", child="", location_env="CLAUDE_CONFIG_DIR"),
    "traycer_local": ConnectionSpec("Traycer", "database_glob", default_traycer_glob, "**/chat/chat.db", "sqlite", ".traycer", "host/epic-state", "chat_projection", ("chat_id", "projection_json")),
    "cursor_local": ConnectionSpec("Cursor local", "database_glob", default_cursor_glob, "**/sdk-agent-store/*/index.db", "sqlite", ".cursor", "projects", "runs", ("run_id", "agent_id", "usage_json", "model", "finished_at", "updated_at", "created_at")),
    "opencode_local": ConnectionSpec("OpenCode", "source_database", default_opencode_database, shape="opencode", home_name="opencode", child="", table="session", columns=("id", "project_id", "directory", "path", "model", "cost", "tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write", "time_updated"), location_env="XDG_DATA_HOME"),
    "zcode_local": ConnectionSpec("ZCode", "source_database", default_zcode_database, shape="zcode", home_name=".zcode", child="", table="model_usage", columns=("id", "session_id", "provider_id", "model_id", "completed_at", "input_tokens", "output_tokens", "reasoning_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "raw_usage_json", "status")),
    "grok_local": ConnectionSpec("Grok Build", "log_path", default_grok_log, shape="grok", home_name=".grok", child="", location_env="GROK_HOME"),
    "cursor_csv": ConnectionSpec("Cursor CSV", "import_path", lambda: _home() / "BURNRATE-imports", "*.csv", "csv"),
    "antigravity_local": ConnectionSpec("Antigravity populated cache", "import_path", lambda: _home() / "BURNRATE-imports" / "antigravity", "*.jsonl", manual_only=True),
    "openai_admin": ConnectionSpec("OpenAI Admin", "admin_key", lambda: "", shape="api", credential_env="OPENAI_ADMIN_KEY", credential_help="Organization Admin API key created by an organization owner, with organization usage and cost read access. Not a ChatGPT subscription login."),
    "anthropic_admin": ConnectionSpec("Anthropic Admin", "admin_key", lambda: "", shape="api", credential_env="ANTHROPIC_ADMIN_KEY", credential_help="Claude Console organization Admin API key with usage and cost report access (admin role required to create it). Not a Claude subscription login."),
    "cursor_admin": ConnectionSpec("Cursor Admin", "api_key", lambda: "", shape="api", credential_env="CURSOR_API_KEY", credential_help="Team Admin API key from Cursor team dashboard settings, created by a team admin. Reads team usage; not a Cursor session token."),
    "openrouter": ConnectionSpec("OpenRouter balance", "api_key", lambda: "", shape="api", credential_env="OPENROUTER_MANAGEMENT_KEY", credential_help="OpenRouter management key with credits access. Provides account balance only, not usage events or subscription quota."),
}


@dataclass(frozen=True)
class ProviderSpec:
    """One ingest source (or quota-only lane) and its capability report."""

    key: str
    ingest_import: str
    capabilities: frozenset[str]
    stability: str
    exactness: str
    enabled_by_default: bool
    ingest: IngestFn | None = None
    kind: IngestKind | None = None
    scheduler_alias: str | None = None
    ingest_kwargs: KwargsFn | None = None
    skip_if: SkipFn | None = None
    connection: ConnectionSpec | None = None
    capability_exactness: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        caps = frozenset(self.capabilities)
        unknown = caps - CAPABILITIES
        if unknown:
            raise ValueError(f"{self.key}: unknown capabilities {sorted(unknown)}")
        if self.stability not in STABILITIES:
            raise ValueError(f"{self.key}: stability must be official|experimental")
        if self.exactness not in EXACTNESS:
            raise ValueError(f"{self.key}: exactness must be exact|derived|partial|unavailable")
        extras = self.capability_exactness or {}
        bad = {name: value for name, value in extras.items() if value not in EXACTNESS}
        if bad:
            raise ValueError(f"{self.key}: invalid capability exactness {bad}")
        object.__setattr__(self, "capabilities", caps)

    def exactness_for(self, capability: str) -> str:
        if capability not in CAPABILITIES:
            raise ValueError(f"unknown capability {capability!r}")
        if capability not in self.capabilities:
            return "unavailable"
        extras = self.capability_exactness or {}
        if capability in extras:
            return extras[capability]
        return self.exactness

    def as_report(self) -> dict:
        return {
            "key": self.key,
            "ingest": self.ingest_import or None,
            "capabilities": {name: self.exactness_for(name) for name in CAPABILITY_NAMES},
            "stability": self.stability,
            "exactness": self.exactness,
            "enabled_by_default": self.enabled_by_default,
        }

    def build_ingest_kwargs(self, settings: Settings, pricing: PricingEngine, **window) -> dict:
        if self.ingest_kwargs is None:
            return _base_kwargs(settings, pricing)
        return self.ingest_kwargs(settings, pricing, **window)


def _spec(
    key: str,
    ingest_import: str,
    capabilities: Iterable[str],
    *,
    stability: str,
    exactness: str,
    enabled_by_default: bool,
    ingest: IngestFn | None = None,
    kind: str | None = None,
    scheduler_alias: str | None = None,
    ingest_kwargs: KwargsFn | None = None,
    skip_if: SkipFn | None = None,
    capability_exactness: Mapping[str, str] | None = None,
) -> ProviderSpec:
    return ProviderSpec(
        key=key,
        connection=CONNECTION_SPECS.get(key),
        ingest_import=ingest_import,
        capabilities=frozenset(capabilities),
        stability=stability,
        exactness=exactness,
        enabled_by_default=enabled_by_default,
        ingest=ingest,
        kind=kind,
        scheduler_alias=scheduler_alias,
        ingest_kwargs=ingest_kwargs,
        skip_if=skip_if,
        capability_exactness=capability_exactness,
    )


# Local ingest order is the current scheduler sequence and must stay stable:
# official readers, then experimental local sources that already run on the
# 15s job. enabled_by_default documents public intent; I2/I6 own runtime flags.
PROVIDERS: tuple[ProviderSpec, ...] = (
    _spec(
        "codex_local",
        "spend_app.adapters.codex_local:ingest",
        ("usage", "quota", "activity"),
        stability="official",
        exactness="exact",
        enabled_by_default=True,
        ingest=ingest_codex_local,
        kind="local",
        scheduler_alias="ingest_codex_local",
        ingest_kwargs=_codex_kwargs,
    ),
    _spec(
        "claude_local",
        "spend_app.adapters.claude_local:ingest",
        ("usage", "quota", "activity"),
        stability="official",
        exactness="exact",
        enabled_by_default=True,
        ingest=ingest_claude_local,
        kind="local",
        scheduler_alias="ingest_claude_local",
        ingest_kwargs=_claude_kwargs,
    ),
    _spec(
        "traycer_local",
        "spend_app.adapters.traycer_local:ingest",
        ("usage", "activity"),
        stability="experimental",
        exactness="partial",
        enabled_by_default=False,
        ingest=ingest_traycer_local,
        kind="local",
        scheduler_alias="ingest_traycer_local",
        ingest_kwargs=_traycer_kwargs,
    ),
    _spec(
        "cursor_local",
        "spend_app.adapters.cursor_local:ingest",
        ("usage", "activity"),
        stability="experimental",
        exactness="derived",
        enabled_by_default=False,
        ingest=ingest_cursor_local,
        kind="local",
        scheduler_alias="ingest_cursor_local",
        ingest_kwargs=_cursor_local_kwargs,
    ),
    _spec(
        "cursor_usage_service",
        "spend_app.adapters.cursor_usage:ingest",
        ("usage", "quota"),
        stability="experimental",
        exactness="derived",
        enabled_by_default=False,
        ingest=ingest_cursor_usage,
        kind="local",
        scheduler_alias="ingest_cursor_usage",
        ingest_kwargs=_pricing_only_kwargs,
        capability_exactness={"quota": "exact"},
        skip_if=_skip_cursor_usage_when_admin_enabled,
    ),
    _spec(
        "opencode_local",
        "spend_app.adapters.opencode_local:ingest",
        ("usage", "quota"),
        stability="official",
        exactness="partial",
        enabled_by_default=True,
        ingest=ingest_opencode_local,
        kind="local",
        scheduler_alias="ingest_opencode_local",
        ingest_kwargs=_opencode_kwargs,
        capability_exactness={"quota": "exact"},
    ),
    _spec(
        "zcode_local",
        "spend_app.adapters.zcode_local:ingest",
        ("usage", "activity"),
        stability="official",
        exactness="derived",
        enabled_by_default=True,
        ingest=ingest_zcode_local,
        kind="local",
        scheduler_alias="ingest_zcode_local",
        ingest_kwargs=_zcode_kwargs,
        capability_exactness={"activity": "exact"},
    ),
    _spec(
        "grok_local",
        "spend_app.adapters.grok_local:ingest",
        ("usage", "quota", "activity"),
        stability="experimental",
        exactness="derived",
        enabled_by_default=False,
        ingest=ingest_grok_local,
        kind="local",
        scheduler_alias="ingest_grok_local",
        ingest_kwargs=_grok_kwargs,
        capability_exactness={"quota": "exact"},
    ),
    _spec(
        "antigravity_local",
        "spend_app.adapters.antigravity_local:ingest",
        ("usage", "quota", "activity"),
        stability="experimental",
        exactness="derived",
        enabled_by_default=False,
        ingest=ingest_antigravity_local,
        kind="local",
        scheduler_alias="ingest_antigravity_local",
        ingest_kwargs=_pricing_only_kwargs,
        capability_exactness={"quota": "exact", "activity": "exact"},
    ),
    _spec(
        "openai_admin",
        "spend_app.adapters.openai_admin:ingest",
        ("usage", "admin"),
        stability="official",
        exactness="exact",
        enabled_by_default=True,
        ingest=ingest_openai_admin,
        kind="admin",
        scheduler_alias="ingest_openai_admin",
        ingest_kwargs=_openai_admin_kwargs,
    ),
    _spec(
        "anthropic_admin",
        "spend_app.adapters.anthropic_admin:ingest",
        ("usage", "admin"),
        stability="official",
        exactness="exact",
        enabled_by_default=True,
        ingest=ingest_anthropic_admin,
        kind="admin",
        scheduler_alias="ingest_anthropic_admin",
        ingest_kwargs=_anthropic_admin_kwargs,
    ),
    _spec(
        "cursor_admin",
        "spend_app.adapters.cursor_admin:ingest",
        ("usage", "admin"),
        stability="official",
        exactness="derived",
        enabled_by_default=True,
        ingest=ingest_cursor_admin,
        kind="admin",
        scheduler_alias="ingest_cursor_admin",
        ingest_kwargs=_cursor_admin_kwargs,
        skip_if=_skip_without_cursor_key,
    ),
    _spec(
        "cursor_csv",
        "spend_app.adapters.cursor_csv:ingest",
        ("usage",),
        stability="official",
        exactness="derived",
        enabled_by_default=False,
        ingest=ingest_cursor_csv,
        kind="manual",
        scheduler_alias="ingest_cursor_csv",
        ingest_kwargs=_cursor_csv_kwargs,
    ),
    _spec(
        "openrouter",
        "",
        ("quota",),
        stability="official",
        exactness="exact",
        enabled_by_default=True,
    ),
    _spec(
        "xai",
        "",
        (),
        stability="experimental",
        exactness="unavailable",
        enabled_by_default=False,
    ),
)


class ProviderRegistry:
    """Live view of ``PROVIDERS`` so tests can monkeypatch the tuple."""

    def __iter__(self):
        return iter(PROVIDERS)

    def __len__(self) -> int:
        return len(PROVIDERS)

    def get(self, key: str) -> ProviderSpec | None:
        for spec in PROVIDERS:
            if spec.key == key:
                return spec
        return None

    def local_ingest(self) -> tuple[ProviderSpec, ...]:
        return tuple(spec for spec in PROVIDERS if spec.kind == "local" and spec.ingest is not None)

    def admin_ingest(self) -> tuple[ProviderSpec, ...]:
        return tuple(spec for spec in PROVIDERS if spec.kind == "admin" and spec.ingest is not None)

    def reports(self) -> list[dict]:
        return [spec.as_report() for spec in PROVIDERS]


REGISTRY = ProviderRegistry()


def get_provider(key: str) -> ProviderSpec | None:
    return REGISTRY.get(key)


def capability_reports() -> list[dict]:
    return REGISTRY.reports()


def iter_local_ingest(
    settings: Settings, pricing: PricingEngine
) -> Sequence[tuple[ProviderSpec, dict]]:
    jobs = []
    for spec in REGISTRY.local_ingest():
        if spec.skip_if is not None and spec.skip_if(settings):
            continue
        jobs.append((spec, spec.build_ingest_kwargs(settings, pricing)))
    return tuple(jobs)


def iter_admin_ingest(
    settings: Settings, pricing: PricingEngine, *, start, end
) -> Sequence[tuple[ProviderSpec, dict]]:
    jobs = []
    for spec in REGISTRY.admin_ingest():
        if spec.skip_if is not None and spec.skip_if(settings):
            continue
        jobs.append((spec, spec.build_ingest_kwargs(settings, pricing, start=start, end=end)))
    return tuple(jobs)


__all__ = (
    "CAPABILITIES",
    "CAPABILITY_NAMES",
    "EXACTNESS",
    "PROVIDERS",
    "REGISTRY",
    "STABILITIES",
    "Capability",
    "Exactness",
    "IngestKind",
    "ProviderRegistry",
    "ProviderSpec",
    "Stability",
    "capability_reports",
    "get_provider",
    "ingest_anthropic_admin",
    "ingest_antigravity_local",
    "ingest_claude_local",
    "ingest_codex_local",
    "ingest_cursor_admin",
    "ingest_cursor_csv",
    "ingest_cursor_local",
    "ingest_cursor_usage",
    "ingest_grok_local",
    "ingest_opencode_local",
    "ingest_openai_admin",
    "ingest_traycer_local",
    "ingest_zcode_local",
    "iter_admin_ingest",
    "iter_local_ingest",
)
