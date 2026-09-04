"""Exact API keys, windows, and bucket counts from the original BURNRATE prompt."""

from __future__ import annotations

from pathlib import Path

from spend_app.config import LOCAL_INGEST_INTERVAL_SECONDS
from tests_spend.test_build_prompt_harness import (
    CONTRACT,
    client_for,
    complete_world,
    require_keys,
    sources,
)


def test_summary_entity_health_carry_every_required_key(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)

    summary = client.get("/api/spend/summary", params={"window": "1d", "tool": "all"})
    assert summary.status_code == 200
    body = summary.json()
    spec = CONTRACT["summaryRequired"]
    require_keys(body, spec["root"], where="summary")
    require_keys(body["window"], spec["window"], where="summary.window")
    require_keys(body["navigation"], spec["navigation"], where="summary.navigation")
    require_keys(body["coverage"], spec["coverage"], where="summary.coverage")
    require_keys(body["totals"], spec["totals"], where="summary.totals")
    require_keys(body["waste"], spec["waste"], where="summary.waste")
    require_keys(body["projected"], spec["projected"], where="summary.projected")
    assert body["generatedAt"]
    assert body["window"]["from"]
    assert body["window"]["to"]
    assert body["cadenceSeconds"] == LOCAL_INGEST_INTERVAL_SECONDS
    assert body["cadenceMinutes"] == LOCAL_INGEST_INTERVAL_SECONDS / 60
    assert isinstance(body["capacity"], list) and body["capacity"]
    require_keys(body["capacity"][0], spec["capacityProvider"], where="capacity[0]")
    assert body["capacity"][0]["rows"]
    require_keys(body["capacity"][0]["rows"][0], spec["capacityRow"], where="capacity.rows[0]")
    assert body["activity"]
    require_keys(body["activity"][0], spec["activity"], where="activity[0]")
    assert body["waste"]["items"]
    require_keys(body["waste"]["items"][0], spec["wasteItem"], where="waste.items[0]")
    require_keys(body["mix"][0], spec["mix"], where="mix[0]")
    require_keys(body["series"][0], spec["series"], where="series[0]")
    require_keys(body["tools"][0], spec["tool"], where="tools[0]")
    require_keys(body["models"][0], spec["model"], where="models[0]")
    require_keys(body["subscriptions"][0], spec["subscription"], where="subscriptions[0]")
    require_keys(body["heatmap"][0], spec["heatmap"], where="heatmap[0]")

    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"},
    )
    assert entity.status_code == 200
    payload = entity.json()
    espec = CONTRACT["entityRequired"]
    require_keys(payload, espec["root"], where="entity")
    require_keys(payload["series"][0], espec["series"], where="entity.series[0]")
    require_keys(payload["opportunity"], espec["opportunity"], where="entity.opportunity")
    require_keys(payload["sessions"], espec["sessions"], where="entity.sessions")
    require_keys(payload["sessions"]["rows"][0], espec["sessionRow"], where="entity.sessions.rows[0]")
    assert payload["generatedAt"]
    assert payload["window"]["from"] and payload["window"]["to"]

    health = client.get("/api/spend/health")
    assert health.status_code == 200
    hbody = health.json()
    hspec = CONTRACT["healthRequired"]
    require_keys(hbody, hspec["root"], where="health")
    assert hbody["ingest"]
    require_keys(hbody["ingest"][0], hspec["ingest"], where="health.ingest[0]")
    assert hbody["quotas"]
    require_keys(hbody["quotas"][0], hspec["quota"], where="health.quotas[0]")


def test_all_twelve_windows_resolve_with_prompt_bucket_counts(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    for spec in CONTRACT["windows"]:
        response = client.get("/api/spend/summary", params={"window": spec["key"]})
        assert response.status_code == 200, spec["key"]
        window = response.json()["window"]
        assert window["key"] == spec["key"]
        assert window["label"] == spec["label"]
        assert window["buckets"] == spec["buckets"], spec
        series = response.json()["series"]
        assert len(series) == spec["buckets"]
        assert window["from"] and window["to"]
        entity = client.get(
            "/api/spend/entity",
            params={"kind": "tool", "key": "codex", "window": spec["key"]},
        )
        assert entity.status_code == 200, spec["key"]
        assert entity.json()["window"]["buckets"] == spec["buckets"]
        assert len(entity.json()["series"]) == spec["buckets"]


def test_window_aliases_and_display_labels_match_the_attached_file(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    for alias, canonical in CONTRACT["windowAliases"].items():
        response = client.get("/api/spend/summary", params={"window": alias})
        assert response.status_code == 200, alias
        assert response.json()["window"]["key"] == canonical
    unknown = client.get("/api/spend/summary", params={"window": "24h"})
    assert unknown.status_code == 400


def test_money_over_the_wire_is_numeric_usd_not_formatted_strings(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    for field in ("trackedValue", "priced", "publishedRate", "meanSessionValue"):
        value = body["totals"][field]
        assert value is None or isinstance(value, (int, float))
        assert "$" not in str(value)
        assert "≈" not in str(value)
    assert isinstance(body["waste"]["perDay"], (int, float))
    assert isinstance(body["waste"]["perMonth"], (int, float))
    assert "$" not in str(body["navigation"]["todayUsd"])
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"},
    ).json()
    assert isinstance(entity["value"], (int, float))
    assert "$" not in str(entity["value"])


def test_frontend_consumes_only_the_three_prompt_payloads() -> None:
    _html, _css, js = sources()
    assert "/api/spend/summary" in js
    assert "/api/spend/entity" in js
    assert "/api/spend/health" in js
    assert "/api/spend/nav" not in js
    assert "/api/spend/limits" not in js
    for key in ("15m", "30m", "1h", "3h", "6h", "12h", "1d", "1w", "1mo", "mtd", "ytd", "all"):
        assert f'key:"{key}"' in js.replace(" ", "")
    assert 'label:"MTD"' in js.replace(" ", "")
    assert 'label:"YTD"' in js.replace(" ", "")
    assert 'label:"All"' in js.replace(" ", "")
    assert "params.get(\"window\")" in js or "params.get('window')" in js
