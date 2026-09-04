from __future__ import annotations

import json
import struct
from pathlib import Path

import httpx

from spend_app.limits import _antigravity_limits_uncached
from spend_app.quotas import antigravity_quota_samples


PAYLOAD = {
    "response": {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "displayName": "Weekly Limit Remaining",
                        "remainingFraction": 0.88,
                        "resetTime": "2026-09-09T17:30:10Z",
                    },
                    {
                        "bucketId": "gemini-5h",
                        "displayName": "Five Hour Limit Remaining",
                        "remainingFraction": 0.35,
                        "resetTime": "2026-09-02T22:30:10Z",
                    },
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {
                        "bucketId": "3p-weekly",
                        "displayName": "Weekly Limit Remaining",
                        "remainingFraction": 1,
                        "resetTime": "2026-09-09T18:55:04Z",
                    },
                    {
                        "bucketId": "3p-5h",
                        "displayName": "Five Hour Limit Remaining",
                        "remainingFraction": 1,
                        "resetTime": "2026-09-02T23:55:04Z",
                    },
                ],
            },
        ]
    }
}


def test_antigravity_local_rpc_reads_grouped_quota_without_exposing_token(
    tmp_path: Path, monkeypatch
) -> None:
    log = tmp_path / "Antigravity" / "logs" / "main.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "Spawning: language_server.exe --csrf_token fixture-local-secret\n"
        "Local: https://127.0.0.1:64566/\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))
    encoded = json.dumps(PAYLOAD).encode()
    framed = b"\x00" + struct.pack(">I", len(encoded)) + encoded

    def fake_post(url, *, content, headers, **kwargs):
        assert url.endswith("/RetrieveUserQuotaSummary")
        assert content == b"\x00\x00\x00\x00\x02{}"
        assert headers["x-codeium-csrf-token"] == "fixture-local-secret"
        assert kwargs["verify"] is False
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, content=framed)

    monkeypatch.setattr("spend_app.limits.httpx.post", fake_post)
    result = _antigravity_limits_uncached()
    assert result["status"] == "exact"
    assert result["key"] == "antigravity"
    assert {row["key"] for row in result["windows"]} == {
        "gemini-weekly",
        "gemini-5h",
        "3p-weekly",
        "3p-5h",
    }
    assert "fixture-local-secret" not in json.dumps(result)


def test_antigravity_quota_samples_store_used_percent_and_real_resets() -> None:
    payload = {
        "status": "exact",
        "windows": [
            {
                "key": bucket["bucketId"],
                "usedPct": (1 - bucket["remainingFraction"]) * 100,
                "resetAt": bucket["resetTime"],
            }
            for group in PAYLOAD["response"]["groups"]
            for bucket in group["buckets"]
        ],
    }
    rows = antigravity_quota_samples(payload, source="antigravity_local_rpc")
    by_key = {row.limit_key: row for row in rows}
    assert by_key["gemini-weekly"].pct == 12
    assert by_key["gemini-5h"].pct == 65
    assert by_key["3p-weekly"].pct == 0
    assert by_key["3p-5h"].pct == 0
    assert by_key["gemini-weekly"].resets_at == "2026-09-09T17:30:10Z"
    assert all(row.source == "antigravity_local_rpc" for row in rows)
