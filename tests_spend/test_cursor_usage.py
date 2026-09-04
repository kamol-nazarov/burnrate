from __future__ import annotations

from datetime import UTC, datetime

from spend_app.adapters.cursor_usage import parse_events


def test_cursor_usage_service_preserves_additive_cache_and_provider_cost() -> None:
    rows = parse_events(
        [
            {
                "timestamp": str(int(datetime(2026, 9, 2, 19, 38, tzinfo=UTC).timestamp() * 1000)),
                "model": "gemini-3.8-flash-high",
                "kind": "composer",
                "conversationId": "conversation-1",
                "chargedCents": 7.130258,
                "tokenUsage": {
                    "inputTokens": 69_218,
                    "cacheReadTokens": 207_981,
                    "outputTokens": 1_083,
                },
            }
        ]
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "cursor_usage_service"
    assert row.tool_key == "cursor"
    assert row.model_key == "cursor:gemini-3.8-flash-high"
    assert row.input_tokens == 69_218 + 207_981
    assert row.cached_input_tokens == 207_981
    assert row.output_tokens == 1_083
    assert row.cost_usd == 0.07130258
    assert row.session_id == "conversation-1"


def test_cursor_usage_service_rejects_pre_cutover_rows() -> None:
    assert parse_events(
        [
            {
                "timestamp": str(int(datetime(2026, 9, 1, 23, 59, tzinfo=UTC).timestamp() * 1000)),
                "model": "gemini-3.8-flash-high",
                "tokenUsage": {"inputTokens": 1},
            }
        ]
    ) == []
