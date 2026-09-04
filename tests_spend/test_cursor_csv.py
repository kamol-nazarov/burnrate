import shutil
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.cursor_csv import ingest as ingest_cursor_csv
from spend_app.adapters.cursor_csv import normalize_header, parse_csv, resolve_columns
from spend_app.db import connect
from spend_app.pricing import PricingEngine


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def test_cursor_csv_drop_maps_documented_columns() -> None:
    rows = parse_csv(FIXTURES / "cursor_usage_events.csv")
    assert len(rows) == 3
    first = rows[0]
    assert first.source == "cursor_csv"
    assert first.tool_key == "cursor"
    assert first.model_key == "cursor:grok-4.6"
    assert first.occurred_at == datetime(2026, 8, 30, 12, tzinfo=UTC)
    # Fresh 100 + cache read 900; cache write stays a separate class.
    assert first.input_tokens == 1000
    assert first.cached_input_tokens == 900
    assert first.cache_write_tokens == 500
    assert first.output_tokens == 200
    assert first.cost_usd == 0.125
    assert first.session_id == "agent_csv_1"
    assert first.raw_id.startswith("cursor-csv:")
    # Row without an agent id still parses with no session identity.
    assert rows[1].session_id is None
    assert rows[1].input_tokens == 550
    assert rows[1].cached_input_tokens == 150
    assert rows[1].raw_id.startswith("cursor-csv:")


def test_cursor_csv_headers_are_tolerant(tmp_path: Path) -> None:
    drop = tmp_path / "usage.csv"
    drop.write_text(
        "\n".join(
            [
                "DATE,USER EMAIL,MODEL NAME,Event ID,Input Tokens,Cache Read Input Tokens,"
                "Cache Creation Tokens,OUTPUT,Charged Cents",
                "2026-08-30T12:00:00.000Z,dev@example.invalid,Grok-4.6,event_9,"
                "100,900,50,200,12.5",
            ]
        ),
        encoding="utf-8",
    )
    rows = parse_csv(drop)
    assert len(rows) == 1
    row = rows[0]
    assert row.model_key == "cursor:grok-4.6"
    assert row.raw_id == "cursor-csv:event_9"
    assert row.input_tokens == 1000
    assert row.cached_input_tokens == 900
    assert row.cache_write_tokens == 50
    assert row.output_tokens == 200
    assert row.cost_usd == 0.125


def test_cursor_csv_column_resolution_prefers_first_alias() -> None:
    fields = resolve_columns(
        ["Date", "User", "Model", "Input With Cache Write Tokens", "Cost (USD)"]
    )
    assert fields == {
        "timestamp": "Date",
        "user": "User",
        "model": "Model",
        "input_with_cache_write": "Input With Cache Write Tokens",
        "cost_usd": "Cost (USD)",
    }
    assert normalize_header("Cost (USD)") == "cost usd"


def test_cursor_csv_ingest_is_idempotent_across_renames(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    import_path = tmp_path / "imports" / "cursor"
    import_path.mkdir(parents=True)
    shutil.copy(FIXTURES / "cursor_usage_events.csv", import_path / "usage-week-34.csv")
    pricing = PricingEngine.load(ROOT / "pricing")

    first = ingest_cursor_csv(
        database_path=database, pricing=pricing, import_path=import_path
    )
    assert first["status"] == "success"
    assert first["files"] == 1
    assert first["eventsWritten"] == 3

    second = ingest_cursor_csv(
        database_path=database, pricing=pricing, import_path=import_path
    )
    assert second["eventsWritten"] == 0

    # Re-dropping the same export under a new file name must not duplicate.
    shutil.copy(FIXTURES / "cursor_usage_events.csv", import_path / "usage-redrop.csv")
    redrop = ingest_cursor_csv(
        database_path=database, pricing=pricing, import_path=import_path
    )
    assert redrop["files"] == 2
    assert redrop["eventsWritten"] == 0

    with connect(database) as connection:
        count, exact = connection.execute(
            "SELECT COUNT(*), MAX(is_exact) FROM usage_events WHERE source='cursor_csv'"
        ).fetchone()
        costs = [
            row[0]
            for row in connection.execute(
                "SELECT cost_usd FROM usage_events WHERE source='cursor_csv' ORDER BY occurred_at"
            )
        ]
    assert count == 3
    # Cursor pricing is derived (published rates), never flagged exact.
    assert exact == 0
    # Charged amounts stay the cost authority.
    assert costs == [0.125, 0.04, 0.02]


def test_cursor_csv_zero_cost_is_factual_zero_not_missing(tmp_path: Path) -> None:
    drop = tmp_path / "usage.csv"
    drop.write_text(
        "\n".join(
            [
                "Date,Model,Event ID,Input Tokens,Cache Read Tokens,Cache Write Tokens,Output Tokens,Cost (USD)",
                "2026-08-30T12:00:00Z,grok-4.6,event_zero,100,0,0,10,0",
            ]
        ),
        encoding="utf-8",
    )
    rows = parse_csv(drop)
    assert len(rows) == 1
    assert rows[0].cost_usd == 0.0
    assert rows[0].raw_id == "cursor-csv:event_zero"


def test_cursor_csv_missing_drop_folder_is_skipped(tmp_path: Path) -> None:
    pricing = PricingEngine.load(ROOT / "pricing")
    result = ingest_cursor_csv(
        database_path=tmp_path / "spend.db",
        pricing=pricing,
        import_path=tmp_path / "missing",
    )
    assert result["status"] == "skipped"
    assert "missing" in result["reason"]
