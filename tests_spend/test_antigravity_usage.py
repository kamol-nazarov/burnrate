from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from spend_app.adapters.antigravity_local import canonical_model, parse_trajectory
from spend_app.pricing import PricingEngine


ROOT = Path(__file__).resolve().parents[1]


def test_parse_trajectory_keeps_exact_additive_cache_semantics() -> None:
    payload = {
        "trajectory": {
            "cascadeId": "conversation-1",
            "steps": [
                {},
                {},
                {
                    "metadata": {
                        "createdAt": "2026-09-02T17:30:13Z",
                        "completedAt": "2026-09-02T17:30:20Z",
                    }
                },
            ],
            "executorMetadatas": [
                {
                    "executionId": "execution-1",
                    "cascadeConfig": {"plannerConfig": {"modelName": "gemini-3.8-flash-high"}},
                }
            ],
            "generatorMetadata": [
                {
                    "executionId": "execution-1",
                    "stepIndices": [2],
                    "chatModel": {
                        "responseModel": "gemini-3.8-flash",
                        "usage": {
                            "inputTokens": "4068",
                            "cacheReadTokens": "16428",
                            "outputTokens": "131",
                            "thinkingOutputTokens": "56",
                            "responseId": "response-1",
                        },
                    },
                }
            ],
        }
    }
    rows = parse_trajectory(
        payload,
        summary={
            "lastModifiedTime": "2026-09-02T17:31:00Z",
            "workspaces": [{"workspaceFolderAbsoluteUri": "file:///C:/Dev/ExampleProject"}],
        },
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.source == "antigravity_local"
    assert row.tool_key == "antigravity"
    assert row.model_key == "antigravity:gemini-3.8-flash-high"
    assert row.occurred_at == datetime(2026, 9, 2, 17, 30, 20, tzinfo=UTC)
    assert row.session_id == "conversation-1"
    assert row.project == "ExampleProject"
    assert row.input_tokens == 4068 + 16428
    assert row.cached_input_tokens == 16428
    assert row.output_tokens == 131
    assert row.reasoning_tokens == 56
    assert row.cache_write_tokens == 0


def test_parse_trajectory_skips_incomplete_generator_and_uses_response_model() -> None:
    payload = {
        "trajectory": {
            "cascadeId": "conversation-2",
            "steps": [],
            "generatorMetadata": [
                {"chatModel": {"usage": {}}},
                {
                    "chatModel": {
                        "responseModel": "claude-sonnet-4-6",
                        "usage": {"inputTokens": "10", "outputTokens": "2", "messageId": "m-2"},
                    }
                },
            ],
        }
    }
    rows = parse_trajectory(payload, summary={"lastModifiedTime": "2026-09-02T18:00:00Z"})
    assert len(rows) == 1
    assert rows[0].model_key == "antigravity:claude-sonnet-4-6"
    assert rows[0].input_tokens == 10
    assert rows[0].cached_input_tokens == 0


def test_canonical_model_never_cross_labels_another_harness() -> None:
    assert canonical_model("gemini-3.8-flash") == "antigravity:gemini-3.8-flash"


def test_gemini_38_flash_published_rate_uses_distinct_cache_price() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    cost = engine.compute(
        model_key="antigravity:gemini-3.8-flash-high",
        occurred_at=datetime(2026, 9, 2, 17, 30, tzinfo=UTC),
        input_tokens=4068 + 16428,
        cached_input_tokens=16428,
        cache_write_tokens=0,
        output_tokens=131,
    )
    expected = (
        Decimal(4068) * Decimal("0.75")
        + Decimal(16428) * Decimal("0.075")
        + Decimal(131) * Decimal("3.75")
    ) / Decimal(1_000_000)
    assert cost == expected
