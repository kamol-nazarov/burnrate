import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spend_app import cli
from spend_app.config import Settings
from spend_app.db import connect


ROOT = Path(__file__).resolve().parents[1]


def _settings(
    tmp_path: Path,
    openai_admin_key: str | None = None,
    anthropic_admin_key: str | None = None,
    cursor_api_key: str | None = None,
) -> Settings:
    return Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=ROOT / "pricing",
        cursor_import_path=tmp_path / "imports",
        anthropic_admin_key=anthropic_admin_key,
        openai_admin_key=openai_admin_key,
        cursor_api_key=cursor_api_key,
        timezone="America/New_York",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40_000,
    )


def test_openai_admin_cli_backfill_skips_without_key(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("sys.argv", ["burnrate", "backfill", "openai-admin"])
    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "skipped"
    assert payload["eventsWritten"] == 0
    assert payload["reason"] == "OPENAI_ADMIN_KEY is not configured"
    dumped = json.dumps(payload)
    assert "Authorization" not in dumped
    assert "Bearer" not in dumped


def test_openai_admin_cli_forwards_bounds_to_ingest(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict = {}

    def fake_ingest(**kwargs):
        captured.update(kwargs)
        return {
            "source": "openai_admin",
            "status": "success",
            "eventsWritten": 0,
            "costBucketsWritten": 0,
        }

    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path, openai_admin_key="configured"))
    monkeypatch.setattr(cli, "ingest_openai_admin", fake_ingest)
    monkeypatch.setattr(
        "sys.argv",
        [
            "burnrate",
            "backfill",
            "openai-admin",
            "--start",
            "2026-08-30T00:00:00Z",
            "--end",
            "2026-08-31T00:00:00Z",
        ],
    )
    assert cli.main() == 0
    assert captured["start"] == datetime(2026, 8, 30, tzinfo=UTC)
    assert captured["end"] == datetime(2026, 8, 31, tzinfo=UTC)
    output = capsys.readouterr().out
    assert "configured" not in output
    assert json.loads(output)["status"] == "success"


def test_anthropic_admin_cli_backfill_skips_without_key(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("sys.argv", ["burnrate", "backfill", "anthropic-admin"])
    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "skipped"
    assert payload["eventsWritten"] == 0
    assert payload["reason"] == "ANTHROPIC_ADMIN_KEY is not configured"
    dumped = json.dumps(payload)
    assert "x-api-key" not in dumped
    assert "sk-ant" not in dumped


def test_anthropic_admin_cli_forwards_bounds_to_ingest(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict = {}

    def fake_ingest(**kwargs):
        captured.update(kwargs)
        return {
            "source": "anthropic_admin",
            "status": "success",
            "eventsWritten": 0,
            "costBucketsWritten": 0,
        }

    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: _settings(tmp_path, anthropic_admin_key="configured-anthropic"),
    )
    monkeypatch.setattr(cli, "ingest_anthropic_admin", fake_ingest)
    monkeypatch.setattr(
        "sys.argv",
        [
            "burnrate",
            "backfill",
            "anthropic-admin",
            "--start",
            "2026-08-30T00:00:00Z",
            "--end",
            "2026-08-31T00:00:00Z",
        ],
    )
    assert cli.main() == 0
    assert captured["start"] == datetime(2026, 8, 30, tzinfo=UTC)
    assert captured["end"] == datetime(2026, 8, 31, tzinfo=UTC)
    assert captured["admin_key"] == "configured-anthropic"
    output = capsys.readouterr().out
    assert "configured-anthropic" not in output
    assert json.loads(output)["status"] == "success"


def test_cli_backfill_choices_include_cursor_csv_and_cursor_admin(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("sys.argv", ["burnrate", "backfill", "cursor-admin"])
    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "skipped"
    assert "not configured" in payload["reason"]
    dumped = json.dumps(payload)
    assert "Authorization" not in dumped
    assert "Basic" not in dumped

    import_path = tmp_path / "imports"
    import_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("sys.argv", ["burnrate", "backfill", "cursor-csv"])
    assert cli.main() == 0
    csv_payload = json.loads(capsys.readouterr().out)
    assert csv_payload["status"] == "skipped"


def test_cli_subscription_add_accepts_quarterly(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(
        "sys.argv",
        [
            "burnrate",
            "subscription",
            "add",
            "--tool-key",
            "custom",
            "--name",
            "Quarterly Plan",
            "--amount-usd",
            "90",
            "--cadence",
            "quarterly",
            "--start-date",
            "2026-08-01",
        ],
    )
    assert cli.main() == 0
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "created"
    with connect(tmp_path / "spend.db") as connection:
        row = connection.execute(
            "SELECT tool_key, name, amount_usd, cadence FROM subscriptions WHERE id=?",
            (created["id"],),
        ).fetchone()
    assert tuple(row) == ("custom", "Quarterly Plan", 90.0, "quarterly")


def test_cursor_admin_cli_forwards_bounds_to_ingest(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict = {}

    def fake_ingest(**kwargs):
        captured.update(kwargs)
        return {"source": "cursor_admin", "status": "success", "eventsWritten": 0}

    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: _settings(tmp_path, cursor_api_key="configured-cursor"),
    )
    monkeypatch.setattr(cli, "ingest_cursor_admin", fake_ingest)
    monkeypatch.setattr(
        "sys.argv",
        [
            "burnrate",
            "backfill",
            "cursor-admin",
            "--start",
            "2026-08-30T00:00:00Z",
            "--end",
            "2026-08-31T00:00:00Z",
        ],
    )
    assert cli.main() == 0
    assert captured["start"] == datetime(2026, 8, 30, tzinfo=UTC)
    assert captured["end"] == datetime(2026, 8, 31, tzinfo=UTC)
    assert captured["api_key"] == "configured-cursor"
    output = capsys.readouterr().out
    assert "configured-cursor" not in output
    assert json.loads(output)["status"] == "success"


def test_cli_omitted_session_glob_uses_per_source_default(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_claude(**kwargs):
        captured.update(kwargs)
        return {"source": "claude_local", "status": "success", "eventsWritten": 0}

    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(cli, "ingest_claude_local", fake_claude)
    monkeypatch.setattr("sys.argv", ["burnrate", "backfill", "claude-local"])
    assert cli.main() == 0
    expected = cli.default_source_path("claude-local", _settings(tmp_path))
    assert captured["session_glob"] == expected
    assert ".claude" in captured["session_glob"]
    assert ".codex" not in captured["session_glob"]


def test_cli_prog_name_is_burnrate(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["burnrate", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: burnrate" in out
    assert "init" in out
    assert "doctor" in out
    assert "serve" in out
    assert "subscription" in out
    assert "vehicledesk-spend" not in out


def test_python_m_spend_app_uses_cli_main() -> None:
    from spend_app.cli import main as cli_main
    import spend_app.__main__ as main_mod

    assert main_mod.main is cli_main


def test_cli_init(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("sys.argv", ["burnrate", "init"])
    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "initialized"
    assert payload["database"] == str(tmp_path / "spend.db")
    assert payload["dataRoot"] == str(tmp_path)
    assert (tmp_path / "spend.db").exists()


def test_cli_init_db_alias(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("sys.argv", ["burnrate", "init-db"])
    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "initialized"


def test_cli_doctor(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("sys.argv", ["burnrate", "doctor"])
    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    by_id = {check["id"]: check for check in payload["checks"]}
    for key in ("python", "imports", "tzdata", "database", "pricing", "web_assets", "bind"):
        assert by_id[key]["status"] == "ok", by_id[key]
    assert "127.0.0.1" in by_id["bind"]["detail"]
    assert "17331" in by_id["bind"]["detail"]


def test_cli_doctor_does_not_import_api() -> None:
    source = Path(cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("spend_app.api"):
            raise AssertionError("cli.py must not import spend_app.api")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("spend_app.api"):
                    raise AssertionError("cli.py must not import spend_app.api")
    assert "spend_app.api:app" in source


def test_cli_serve_binds_localhost(monkeypatch, capsys) -> None:
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setattr("sys.argv", ["burnrate", "serve"])
    assert cli.main() == 0
    assert captured["args"][0] == "spend_app.api:app"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 17331
    assert "127.0.0.1:17331" in capsys.readouterr().out


def test_cli_serve_refuses_wildcard_bind(monkeypatch, capsys) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("uvicorn.run must not be called for 0.0.0.0")

    monkeypatch.setattr("uvicorn.run", boom)
    monkeypatch.setattr("sys.argv", ["burnrate", "serve", "--host", "0.0.0.0"])
    assert cli.main() == 2
    err = capsys.readouterr().err
    assert "0.0.0.0" in err
    assert "127.0.0.1" in err


def test_cli_subscription_list(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(
        "sys.argv",
        [
            "burnrate",
            "subscription",
            "add",
            "--tool-key",
            "custom",
            "--name",
            "Listed Plan",
            "--amount-usd",
            "12",
            "--cadence",
            "monthly",
            "--start-date",
            "2026-08-01",
        ],
    )
    assert cli.main() == 0
    created = json.loads(capsys.readouterr().out)
    monkeypatch.setattr("sys.argv", ["burnrate", "subscription", "list"])
    assert cli.main() == 0
    rows = json.loads(capsys.readouterr().out)
    assert any(row["id"] == created["id"] and row["name"] == "Listed Plan" for row in rows)
