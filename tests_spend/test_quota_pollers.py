from datetime import UTC, datetime
from pathlib import Path

import spend_app.limits as limits
from spend_app.db import connect, initialize
from spend_app.quotas import (
    PRESSURE_AMBER,
    PRESSURE_BLUE,
    PRESSURE_GREEN,
    PRESSURE_RED,
    REQUIRED_LIMITS,
    QuotaSample,
    agent_run_records,
    claude_quota_samples,
    codex_quota_samples,
    cursor_quota_samples,
    default_quota_collectors,
    derived_used,
    grok_quota_samples,
    is_live_run,
    openrouter_quota_samples,
    order_capacity_rows,
    poll_activity,
    poll_quotas,
    pressure_color,
    quota_note,
    split_quota_label,
    zai_quota_samples,
)


NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)
POLLED = "2026-09-15T12:00:00Z"
POLLED_LATER = "2026-09-15T13:00:00Z"

CLAUDE_PAYLOAD = {
    "key": "claude-code",
    "name": "Claude Code",
    "plan": "Claude Max",
    "status": "exact",
    "observedAt": "2026-08-31T12:00:00Z",
    "windows": [
        {
            "key": "5h",
            "label": "5-hour window",
            "usedPct": 12.5,
            "remainingPct": 87.5,
            "resetAt": "2026-08-31T17:00:00Z",
        },
        {
            "key": "weekly",
            "label": "Weekly · all models",
            "usedPct": 41,
            "remainingPct": 59,
            "resetAt": "2026-09-04T23:59:59Z",
        },
    ],
    "detail": "Traycer read-only Claude provider quota; polled adaptively.",
}

GROK_PAYLOAD = {
    "key": "grok",
    "name": "Grok Build",
    "plan": "SuperGrok Heavy",
    "status": "exact",
    "observedAt": "2026-08-31T12:00:00Z",
    "windows": [
        {
            "key": "weekly",
            "label": "Weekly Grok Build",
            "usedPct": 26,
            "remainingPct": 74,
            "resetAt": "2026-09-02T00:20:35.074000+00:00",
        }
    ],
    "detail": "Traycer read-only Grok Build provider quota; polled adaptively.",
}

CODEX_PAYLOAD = {
    "key": "codex",
    "name": "Codex",
    "plan": "ChatGPT Pro",
    "status": "exact",
    "observedAt": "2026-08-31T12:05:38Z",
    "windows": [
        {
            "key": "primary",
            "label": "Primary · 5h",
            "windowMinutes": 300,
            "usedPct": 11,
            "remainingPct": 89,
            "resetAt": "2026-08-31T16:00:00Z",
        },
        {
            "key": "secondary",
            "label": "Secondary · 7d",
            "windowMinutes": 10080,
            "usedPct": 33.5,
            "remainingPct": 66.5,
            "resetAt": "2026-09-06T12:05:38Z",
        },
    ],
    "detail": "Native Codex rate-limit telemetry.",
}

CURSOR_PAYLOAD = {
    "key": "cursor",
    "name": "Cursor",
    "plan": "Cursor Pro",
    "status": "exact",
    "observedAt": "2026-08-31T12:00:00Z",
    "windows": [
        {"key": "included", "label": "Included value", "usedPct": 22.0},
        {"key": "cursor_models", "label": "Cursor Models", "usedPct": 55.0},
        {"key": "other_models", "label": "Other Models", "usedPct": 4.0},
    ],
    "detail": "Cursor authenticated read-only usage service.",
}

ZAI_PAYLOAD = {
    "key": "opencode",
    "name": "Z.AI / OpenCode",
    "plan": "Max",
    "status": "exact",
    "observedAt": "2026-08-31T12:00:00Z",
    "windows": [
        {
            "key": "5h",
            "label": "5-hour credits",
            "usedPct": 30.0,
            "used": 42000,
            "limit": 140000,
            "remaining": 98000,
            "resetAt": "2026-08-31T16:00:00.000Z",
        },
        {
            "key": "weekly",
            "label": "Weekly credits",
            "usedPct": 18.5,
            "used": 25900,
            "limit": 140000,
            "remaining": 114100,
            "resetAt": "2026-09-06T00:00:00.000Z",
        },
    ],
    "detail": "Official Z.AI Coding Plan quota endpoint.",
}

OPENROUTER_PAYLOAD = {
    "key": "openrouter",
    "name": "OpenRouter",
    "plan": "PAYG",
    "status": "exact",
    "windows": [
        {
            "key": "balance",
            "remainingUsd": 75.0,
            "totalCreditsUsd": 100.0,
            "totalUsageUsd": 25.0,
        }
    ],
    "detail": "OpenRouter account credits from the read-only management endpoint.",
}


def fixture_collectors(database_path: Path) -> dict:
    return {
        "claude-code": lambda: claude_quota_samples(CLAUDE_PAYLOAD, source="traycer_profile"),
        "codex": lambda: codex_quota_samples(CODEX_PAYLOAD, source="codex_local_telemetry"),
        "cursor": lambda: cursor_quota_samples(CURSOR_PAYLOAD, source="cursor_usage_service"),
        "grok": lambda: grok_quota_samples(GROK_PAYLOAD, source="traycer_profile"),
        "opencode": lambda: zai_quota_samples(ZAI_PAYLOAD, source="zai_quota_endpoint"),
        "openrouter": lambda: openrouter_quota_samples(collector=lambda: OPENROUTER_PAYLOAD),
    }


def seed_openrouter_spend(database_path: Path) -> None:
    with connect(database_path) as connection:
        connection.executescript(
            """
            INSERT INTO usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, cost_usd, computed_cost_usd, raw_id, ingested_at
            ) VALUES
            ('traycer_local','openrouter','openrouter:glm-5.3-flash','2026-09-05T10:00:00Z',
                NULL,NULL,10,0,0,0,5,NULL,1.25,0.0,'fixture:or:1','2026-09-05T10:01:00Z'),
            ('traycer_local','openrouter','openrouter:glm-5.3-flash','2026-09-05T11:00:00Z',
                NULL,NULL,10,0,0,0,5,NULL,0.75,0.0,'fixture:or:2','2026-09-05T11:01:00Z'),
            ('traycer_local','openrouter','openrouter:glm-5.3-flash','2026-08-20T10:00:00Z',
                NULL,NULL,10,0,0,0,5,NULL,99.0,0.0,'fixture:or:3','2026-08-20T10:01:00Z');
            INSERT INTO unpriced_usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, unclassified_tokens, telemetry_complete,
                cost_usd, raw_id, ingested_at
            ) VALUES
            ('traycer_local','openrouter','openrouter:glm-5.3-flash','2026-09-06T10:00:00Z',
                NULL,NULL,10,0,0,0,5,NULL,0,1,2.00,'fixture:or:4','2026-09-06T10:01:00Z');
            """
        )


def quota_rows(database_path: Path) -> dict[tuple[str, str], dict]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT provider_key, limit_key, label, used, allowance, unit, pct, "
            "resets_at, source, is_payg, polled_at FROM quotas"
        ).fetchall()
    return {(row[0], row[1]): dict(zip(row.keys(), tuple(row))) for row in rows}


def test_poll_quotas_persists_real_rows_with_undocumented_sources_and_resets(
    tmp_path: Path,
) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    seed_openrouter_spend(database)
    result = poll_quotas(
        database,
        collectors=fixture_collectors(database),
        now=lambda: POLLED,
    )

    assert result["polledAt"] == POLLED
    assert result["written"] == 9
    assert result["skipped"] == 0
    assert result["providers"] == [
        "claude-code",
        "codex",
        "cursor",
        "grok",
        "opencode",
        "openrouter",
    ]

    rows = quota_rows(database)
    assert set(rows) == {
        ("claude-code", "5h"),
        ("claude-code", "weekly"),
        ("codex", "weekly"),
        ("cursor", "cursor_models"),
        ("cursor", "other_models"),
        ("grok", "weekly"),
        ("opencode", "5h"),
        ("opencode", "weekly"),
        ("openrouter", "balance"),
    }

    claude_5h = rows[("claude-code", "5h")]
    assert claude_5h["pct"] == 12.5
    assert claude_5h["used"] is None and claude_5h["allowance"] is None
    assert claude_5h["unit"] == "pct"
    assert claude_5h["source"] == "traycer_profile"
    assert claude_5h["resets_at"] == "2026-08-31T17:00:00Z"
    assert rows[("claude-code", "weekly")]["pct"] == 41.0

    codex = rows[("codex", "weekly")]
    assert codex["pct"] == 33.5
    assert codex["source"] == "codex_local_telemetry"
    assert codex["resets_at"] == "2026-09-06T12:05:38Z"

    cursor_models = rows[("cursor", "cursor_models")]
    other_models = rows[("cursor", "other_models")]
    assert cursor_models["pct"] == 55.0
    assert other_models["pct"] == 4.0
    assert cursor_models["source"] == "cursor_usage_service"

    grok = rows[("grok", "weekly")]
    assert grok["pct"] == 26.0
    assert grok["source"] == "traycer_profile"
    # Reset instants persist at whole-second precision; the provider's
    # sub-second fraction is clock noise, not a different reset time.
    assert grok["resets_at"] == "2026-09-02T00:20:35Z"

    zai_5h = rows[("opencode", "5h")]
    assert zai_5h["unit"] == "credits"
    assert zai_5h["used"] == 42000.0
    assert zai_5h["allowance"] == 140000.0
    assert zai_5h["pct"] == 30.0
    assert zai_5h["source"] == "zai_quota_endpoint"
    assert rows[("opencode", "weekly")]["pct"] == 18.5

    payg = rows[("openrouter", "balance")]
    assert payg["is_payg"] == 1
    assert payg["used"] == 75.0
    assert payg["pct"] is None
    assert payg["allowance"] is None
    assert payg["unit"] == "usd"
    assert payg["label"] == "OpenRouter funds remaining"
    assert ("cursor", "included") not in rows


def test_openrouter_credits_expose_exact_remaining_funds() -> None:
    samples = openrouter_quota_samples(collector=lambda: OPENROUTER_PAYLOAD)
    assert len(samples) == 1
    assert samples[0].used == 75.0
    assert samples[0].is_payg is True
    assert samples[0].pct is None
    assert samples[0].allowance is None


def test_openrouter_credit_reader_keeps_management_key_out_of_result(monkeypatch) -> None:
    secret = "fixture-management-secret"
    observed = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"total_credits": 100.0, "total_usage": 25.5}}

    def fake_get(url, **kwargs):
        observed["url"] = url
        observed["authorization"] = kwargs["headers"]["Authorization"]
        observed["trust_env"] = kwargs.get("trust_env")
        observed["follow_redirects"] = kwargs.get("follow_redirects")
        return Response()

    monkeypatch.setattr(limits.httpx, "get", fake_get)
    payload = limits._openrouter_credits_uncached(secret)
    assert observed == {
        "url": limits.OPENROUTER_CREDITS_URL,
        "authorization": f"Bearer {secret}",
        "trust_env": False,
        "follow_redirects": False,
    }
    assert payload["status"] == "exact"
    assert payload["windows"][0]["remainingUsd"] == 74.5
    assert secret not in str(payload)


def test_openrouter_credit_reader_ignores_inference_key(monkeypatch) -> None:
    observed = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"total_credits": 10.0, "total_usage": 1.0}}

    def fake_get(_url, **kwargs):
        observed["authorization"] = kwargs["headers"]["Authorization"]
        return Response()

    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-inference-secret")
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "fixture-management-secret")
    monkeypatch.setattr(limits.httpx, "get", fake_get)
    payload = limits._openrouter_credits_uncached()
    assert observed["authorization"] == "Bearer fixture-management-secret"
    assert "fixture-inference-secret" not in str(payload)
    assert payload["windows"][0]["remainingUsd"] == 9.0


def test_openrouter_credit_reader_refuses_non_allowlisted_host(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        raise AssertionError("must not send credentials off the OpenRouter host allowlist")

    monkeypatch.setattr(limits, "OPENROUTER_CREDITS_URL", "https://evil.example/api/v1/credits")
    monkeypatch.setattr(limits.httpx, "get", fake_get)
    payload = limits._openrouter_credits_uncached("fixture-management-secret")
    assert calls == []
    assert payload["status"] == "error"
    assert "fixture-management-secret" not in str(payload)


def test_unavailable_sources_persist_reasons_and_never_fake_zero(
    tmp_path: Path,
) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    collectors = {
        "claude-code": lambda: claude_quota_samples(
            {
                "key": "claude-code",
                "status": "unavailable",
                "windows": [],
                "detail": "Claude OAuth credentials are unavailable.",
            },
            source="claude_oauth_usage",
        ),
        "codex": lambda: codex_quota_samples(
            {
                "key": "codex",
                "status": "unavailable",
                "windows": [],
                "detail": "No local rate-limit snapshot was found.",
            },
            source="codex_local_telemetry",
        ),
        "grok": lambda: grok_quota_samples(
            {
                "key": "grok",
                "status": "error",
                "windows": [],
                "detail": "Traycer Grok Build quota lookup failed (RuntimeError).",
            },
            source="traycer_profile",
        ),
        "cursor": lambda: cursor_quota_samples(
            {
                "key": "cursor",
                "status": "exact",
                "windows": [
                    {"key": "cursor_models", "label": "Cursor Models", "usedPct": None},
                    {"key": "other_models", "label": "Other Models", "usedPct": 7.0},
                ],
            },
            source="cursor_usage_service",
        ),
        "opencode": lambda: zai_quota_samples(
            {
                "key": "opencode",
                "status": "exact",
                "windows": [
                    {
                        "key": "weekly",
                        "label": "Weekly credits",
                        "usedPct": 9.0,
                        "used": 100,
                        "limit": 140000,
                    }
                ],
            },
            source="zai_quota_endpoint",
        ),
            "openrouter": lambda: openrouter_quota_samples(
                collector=lambda: {
                    "status": "unavailable",
                    "windows": [],
                    "detail": "OPENROUTER_MANAGEMENT_KEY is not configured.",
                }
            ),
    }
    result = poll_quotas(database, collectors=collectors, now=lambda: POLLED)
    assert result["written"] == 9

    rows = quota_rows(database)
    unavailable = [
        rows[("claude-code", "5h")],
        rows[("claude-code", "weekly")],
        rows[("codex", "weekly")],
        rows[("grok", "weekly")],
        rows[("cursor", "cursor_models")],
        rows[("opencode", "5h")],
            rows[("openrouter", "balance")],
    ]
    for row in unavailable:
        assert row["pct"] is None
        assert row["used"] is None
        assert row["allowance"] is None
        assert row["unit"] == "unavailable"
        name, reason = split_quota_label(row["label"])
        assert name
        assert reason

    assert (
        rows[("claude-code", "5h")]["label"]
        == "Claude 5-hour window — Claude OAuth credentials are unavailable."
    )
    assert (
        rows[("codex", "weekly")]["label"]
        == "Codex weekly window — No local rate-limit snapshot was found."
    )
    assert (
        rows[("grok", "weekly")]["label"]
        == "Grok Build weekly — Traycer Grok Build quota lookup failed (RuntimeError)."
    )
    assert (
        rows[("cursor", "cursor_models")]["label"]
        == "Cursor Models — Experimental Cursor DashboardService omitted the Cursor Models percentage."
    )
    assert (
        rows[("opencode", "5h")]["label"]
        == "Z.AI 5-hour credits — The Z.AI quota endpoint omitted the Z.AI 5-hour credits window."
    )
    assert rows[("cursor", "other_models")]["pct"] == 7.0
    assert rows[("opencode", "weekly")]["pct"] == 9.0

    for row in rows.values():
        assert not (row["pct"] == 0 and row["unit"] == "unavailable")


def test_default_collectors_cover_required_providers_without_invoking() -> None:
    collectors = default_quota_collectors(Path("unused.db"))
    assert set(collectors) == set(REQUIRED_LIMITS)
    assert set(collectors) == {
        "antigravity",
        "claude-code",
        "codex",
        "cursor",
        "grok",
        "opencode",
        "openrouter",
    }


def test_poll_quotas_public_seam_uses_default_collectors(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    called: list[Path] = []

    def fake_collectors(path: Path) -> dict:
        called.append(path)
        return fixture_collectors(path)

    monkeypatch.setattr("spend_app.quotas.default_quota_collectors", fake_collectors)
    result = poll_quotas(database, now=lambda: POLLED)
    assert called == [database]
    assert result["written"] == 9
    assert ("openrouter", "balance") in quota_rows(database)


def test_omitted_required_limit_is_persisted_unavailable(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    result = poll_quotas(
        database,
        collectors={"claude-code": lambda: []},
        now=lambda: POLLED,
    )
    assert result["written"] == 2
    rows = quota_rows(database)
    assert rows[("claude-code", "5h")]["unit"] == "unavailable"
    assert rows[("claude-code", "weekly")]["unit"] == "unavailable"
    _, reason = split_quota_label(rows[("claude-code", "5h")]["label"])
    assert reason == "No sample was produced for this limit during the poll."


def test_failing_collector_persists_unavailable_rows(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)

    def broken() -> list[QuotaSample]:
        raise RuntimeError("provider socket exploded")

    result = poll_quotas(
        database,
        collectors={"grok": broken},
        now=lambda: POLLED,
    )
    assert result["written"] == 1
    rows = quota_rows(database)
    row = rows[("grok", "weekly")]
    assert row["unit"] == "unavailable"
    assert row["pct"] is None
    _, reason = split_quota_label(row["label"])
    assert reason == "Quota collection failed (RuntimeError)."


def test_one_collector_failure_does_not_stop_other_lanes(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)

    def broken() -> list[QuotaSample]:
        raise RuntimeError("provider socket exploded")

    result = poll_quotas(
        database,
        collectors={
            "grok": broken,
            "codex": lambda: codex_quota_samples(CODEX_PAYLOAD, source="codex_local_telemetry"),
        },
        now=lambda: POLLED,
    )
    assert result["written"] == 2
    rows = quota_rows(database)
    assert rows[("grok", "weekly")]["unit"] == "unavailable"
    assert rows[("codex", "weekly")]["pct"] == 33.5
    assert rows[("codex", "weekly")]["unit"] == "pct"


def test_identical_poll_is_idempotent_and_writes_no_new_rows(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    seed_openrouter_spend(database)
    collectors = fixture_collectors(database)
    first = poll_quotas(database, collectors=collectors, now=lambda: POLLED)
    assert first["written"] == 9

    second = poll_quotas(database, collectors=collectors, now=lambda: POLLED_LATER)
    assert second["written"] == 0
    assert second["skipped"] == 9

    rows = quota_rows(database)
    assert len(rows) == 9
    # No new history row, but each row now names the latest poll that
    # confirmed its value so /health lastPoll reflects the real poll time.
    assert all(row["polled_at"] == POLLED_LATER for row in rows.values())


def test_changed_value_writes_only_one_new_row(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    seed_openrouter_spend(database)
    collectors = fixture_collectors(database)
    poll_quotas(database, collectors=collectors, now=lambda: POLLED)

    changed = dict(GROK_PAYLOAD)
    changed["windows"] = [
        {**GROK_PAYLOAD["windows"][0], "usedPct": 30}
    ]
    collectors["grok"] = lambda: grok_quota_samples(changed, source="traycer_profile")
    result = poll_quotas(database, collectors=collectors, now=lambda: POLLED_LATER)

    assert result["written"] == 1
    rows = quota_rows(database)
    assert len(rows) == 9
    with connect(database) as connection:
        grok_rows = connection.execute(
            "SELECT pct, polled_at FROM quotas WHERE provider_key='grok' ORDER BY polled_at"
        ).fetchall()
    assert len(grok_rows) == 2
    assert grok_rows[0][0] == 26.0 and grok_rows[0][1] == POLLED
    assert grok_rows[1][0] == 30.0 and grok_rows[1][1] == POLLED_LATER


def test_pressure_color_boundaries() -> None:
    assert pressure_color(None) == "unavailable"
    assert pressure_color(0) == PRESSURE_GREEN
    assert pressure_color(29.9) == PRESSURE_GREEN
    assert pressure_color(30) == PRESSURE_BLUE
    assert pressure_color(59.9) == PRESSURE_BLUE
    assert pressure_color(60) == PRESSURE_AMBER
    assert pressure_color(84.9) == PRESSURE_AMBER
    assert pressure_color(85) == PRESSURE_RED
    assert pressure_color(100) == PRESSURE_RED
    assert PRESSURE_GREEN == "#63c689"
    assert PRESSURE_BLUE == "#78a8f8"
    assert PRESSURE_AMBER == "#d9a441"
    assert PRESSURE_RED == "#dc6c78"


def test_capacity_order_uses_semantic_primary_rows_and_openrouter_last() -> None:
    rows = [
        QuotaSample(
            provider_key="openrouter",
            limit_key="balance",
            label="OpenRouter funds remaining",
            unit="usd",
            source="openrouter_credits_api",
            used=4.0,
            is_payg=True,
        ),
        QuotaSample(
            provider_key="grok",
            limit_key="weekly",
            label="Grok Build weekly",
            unit="pct",
            source="traycer_profile",
            pct=100.0,
        ),
        QuotaSample(
            provider_key="claude-code",
            limit_key="weekly",
            label="Claude weekly window",
            unit="pct",
            source="traycer_profile",
            pct=41.0,
        ),
        QuotaSample(
            provider_key="claude-code",
            limit_key="5h",
            label="Claude 5-hour window",
            unit="pct",
            source="traycer_profile",
            pct=12.5,
        ),
        QuotaSample(
            provider_key="codex",
            limit_key="weekly",
            label="Codex weekly window",
            unit="pct",
            source="codex_local_telemetry",
        ),
        QuotaSample(
            provider_key="opencode",
            limit_key="weekly",
            label="Z.AI weekly credits",
            unit="credits",
            source="zai_quota_endpoint",
            pct=18.5,
        ),
    ]
    ordered = order_capacity_rows(rows)
    assert [(row.provider_key, row.limit_key) for row in ordered] == [
        ("grok", "weekly"),
        ("opencode", "weekly"),
        ("claude-code", "5h"),
        ("claude-code", "weekly"),
        ("codex", "weekly"),
        ("openrouter", "balance"),
    ]


def test_quota_note_derives_only_from_row_values() -> None:
    assert quota_note("usd", used=4.0) == "$4.00 spent"
    assert quota_note("usd", used=12.5, allowance=40) == "$12.50 of $40.00 spent"
    assert quota_note("credits", used=42000, allowance=140000) == (
        "42,000 of 140,000 credits used"
    )
    assert quota_note("credits", pct=18.5) == "18.5% of credits used"
    assert quota_note("pct", pct=41) == "41% used"
    assert quota_note("pct", pct=33.5) == "33.5% used"
    assert quota_note("unavailable", label="Claude 5-hour window — no live data") == (
        "no live data"
    )
    assert quota_note("pct") is None
    assert quota_note("usd") is None
    assert quota_note("unknown", used=1) is None
    assert derived_used(used=1, allowance=140000, pct=30) == 42000.0
    assert quota_note("credits", used=1, allowance=140000, pct=30) == (
        "42,000 of 140,000 credits used"
    )
    assert quota_note("usd", used=9, allowance=40, pct=31.25) == "$12.50 of $40.00 spent"


def test_split_quota_label_round_trip() -> None:
    assert split_quota_label("Codex weekly window") == ("Codex weekly window", None)
    name, reason = split_quota_label("Grok Build weekly — source did not answer")
    assert name == "Grok Build weekly"
    assert reason == "source did not answer"


ACTIVITY = {
    "activeAgents": [
        {
            "chatId": "chat-1",
            "title": "Implement quotas",
            "harness": "codex",
            "model": "gpt-5.6",
            "startedAt": "2026-09-15T12:00:00Z",
            "usageStatus": "pending_turn_completion",
        }
    ],
    "unmeteredTurns": [
        {
            "chatId": "chat-2",
            "title": "Review diff",
            "harness": "grok",
            "model": "grok-4.6",
            "startedAt": "2026-09-15T11:00:00Z",
            "stoppedAt": "2026-09-15T11:04:00Z",
            "usageStatus": "stopped_without_usage",
        }
    ],
}


def test_poll_activity_persists_live_and_no_data_runs(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    result = poll_activity(
        database,
        collector=lambda: ACTIVITY,
        now=lambda: "2026-09-15T12:01:00Z",
    )
    assert result["seen"] == 2
    assert result["new"] == 2
    assert result["live"] == 1
    assert result["noData"] == 1

    with connect(database) as connection:
        rows = {
            row[0]: tuple(row)
            for row in connection.execute(
                "SELECT id, name, model_key, state, started_at, last_seen_at FROM agent_runs"
            ).fetchall()
        }
    assert rows["traycer:chat-1"] == (
        "traycer:chat-1",
        "Implement quotas",
        "gpt-5.6",
        "live",
        "2026-09-15T12:00:00Z",
        "2026-09-15T12:00:00Z",
    )
    assert rows["traycer:chat-2"] == (
        "traycer:chat-2",
        "Review diff",
        "grok-4.6",
        "no_data",
        "2026-09-15T11:00:00Z",
        "2026-09-15T11:04:00Z",
    )

    assert is_live_run("live") is True
    assert is_live_run("LIVE") is True
    assert is_live_run("running") is True
    assert is_live_run("no_data") is False
    assert is_live_run("NO DATA") is False


def test_poll_activity_repeated_poll_does_not_duplicate_and_updates_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "spend.db"
    poll_activity(database, collector=lambda: ACTIVITY, now=lambda: POLLED)

    second = poll_activity(
        database, collector=lambda: ACTIVITY, now=lambda: POLLED_LATER
    )
    assert second["seen"] == 2
    assert second["new"] == 0

    stopped = {
        "activeAgents": [],
        "unmeteredTurns": [
            {
                **ACTIVITY["activeAgents"][0],
                "stoppedAt": "2026-09-15T12:05:00Z",
                "usageStatus": "stopped_without_usage",
            }
        ],
    }
    third = poll_activity(database, collector=lambda: stopped, now=lambda: POLLED_LATER)
    assert third["live"] == 0
    assert third["noData"] == 1

    with connect(database) as connection:
        rows = connection.execute(
            "SELECT id, state, last_seen_at FROM agent_runs"
        ).fetchall()
    assert len(rows) == 2
    states = {row[0]: row[1] for row in rows}
    assert states["traycer:chat-1"] == "no_data"
    assert states["traycer:chat-2"] == "no_data"


def test_agent_run_records_skip_invalid_entries() -> None:
    records = agent_run_records(
        {
            "activeAgents": [
                {"chatId": "", "title": "x", "startedAt": "2026-09-15T12:00:00Z"},
                {"title": "no chat id", "startedAt": "2026-09-15T12:00:00Z"},
                {"chatId": "chat-9", "title": "no timestamps"},
            ],
            "unmeteredTurns": ["not-a-dict"],
        }
    )
    assert records == []


def test_factual_zero_percent_is_persisted_not_unavailable(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    payload = {
        **CLAUDE_PAYLOAD,
        "windows": [
            {**CLAUDE_PAYLOAD["windows"][0], "usedPct": 0, "remainingPct": 100},
            CLAUDE_PAYLOAD["windows"][1],
        ],
    }
    poll_quotas(
        database,
        collectors={
            "claude-code": lambda: claude_quota_samples(payload, source="traycer_profile")
        },
        now=lambda: POLLED,
    )
    rows = quota_rows(database)
    zero = rows[("claude-code", "5h")]
    assert zero["pct"] == 0
    assert zero["unit"] == "pct"
    assert zero["used"] is None
    assert "unavailable" not in zero["label"]


def test_poll_activity_clears_stale_live_runs_and_keeps_no_data(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    poll_activity(database, collector=lambda: ACTIVITY, now=lambda: POLLED)
    emptied = poll_activity(
        database,
        collector=lambda: {"activeAgents": [], "unmeteredTurns": []},
        now=lambda: POLLED_LATER,
    )
    assert emptied["seen"] == 0
    assert emptied["live"] == 0
    assert emptied["cleared"] == 1
    with connect(database) as connection:
        rows = {
            row[0]: row[1]
            for row in connection.execute("SELECT id, state FROM agent_runs")
        }
    assert "traycer:chat-1" not in rows
    assert rows["traycer:chat-2"] == "no_data"
    assert "live" not in set(rows.values())
    assert "running" not in set(rows.values())


def test_poll_quotas_fixture_collectors_do_not_call_providers(
    tmp_path: Path, monkeypatch
) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("quota tests must not call providers or credentials")

    monkeypatch.setattr("spend_app.limits.httpx.get", boom)
    monkeypatch.setattr("spend_app.limits.httpx.Client", boom)
    monkeypatch.setattr("spend_app.limits.subprocess.run", boom)
    monkeypatch.setattr("spend_app.limits.Path.home", boom)
    database = tmp_path / "spend.db"
    initialize(database)
    seed_openrouter_spend(database)
    result = poll_quotas(
        database,
        collectors=fixture_collectors(database),
        now=lambda: POLLED,
    )
    assert result["written"] == 9
