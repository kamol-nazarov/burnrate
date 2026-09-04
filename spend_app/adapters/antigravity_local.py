"""Experimental: Antigravity usage from undocumented localhost gRPC-Web.

The desktop client's language-server RPC
(``exa.language_server_pb.LanguageServerService`` on ``https://127.0.0.1:<port>``)
is undocumented and may change without notice. The reader requests the same
PROD_UI trajectory shape used by the app, immediately reduces it to metadata,
and never persists prompt, response, tool, or file content.
"""

from __future__ import annotations

import json
import struct
from datetime import UTC
from pathlib import Path

import httpx

from spend_app.adapters.common import UsageRow, failed_result, persist_rows, skipped_result, stable_id
from spend_app.adapters.local_common import parse_iso_time
from spend_app.limits import (
    ANTIGRAVITY_RPC_ALLOWED_HOSTS,
    _antigravity_local_connection,
    _assert_allowed_https_host,
    _grpc_web_json_payload,
)
from spend_app.pricing import PricingEngine


SOURCE = "antigravity_local"
TOOL_KEY = "antigravity"
MODEL_PREFIX = "antigravity:"
PROD_UI_VERBOSITY = 2
_TRAJECTORY_SIGNATURES: dict[tuple[str, str], tuple[object, ...]] = {}


def reset_state() -> None:
    _TRAJECTORY_SIGNATURES.clear()


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def canonical_model(value: object) -> str:
    model = str(value or "unknown").strip().lower().replace("_", "-")
    return MODEL_PREFIX + (model or "unknown")


def _rpc_json(
    client: httpx.Client,
    *,
    base_url: str,
    csrf_token: str,
    method: str,
    payload: dict,
) -> dict:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    frame = b"\x00" + struct.pack(">I", len(encoded)) + encoded
    response = client.post(
        base_url + "/exa.language_server_pb.LanguageServerService/" + method,
        content=frame,
        headers={
            "x-codeium-csrf-token": csrf_token,
            "content-type": "application/grpc-web+json",
            "x-grpc-web": "1",
        },
    )
    response.raise_for_status()
    return _grpc_web_json_payload(response.content)


def _project_name(summary: dict) -> str | None:
    metadata = summary.get("trajectoryMetadata")
    workspaces = summary.get("workspaces") or (
        metadata.get("workspaces") if isinstance(metadata, dict) else []
    ) or []
    if not workspaces or not isinstance(workspaces[0], dict):
        return None
    uri = workspaces[0].get("workspaceFolderAbsoluteUri") or workspaces[0].get("gitRootAbsoluteUri")
    if not isinstance(uri, str) or not uri.strip():
        return None
    return uri.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or None


def parse_trajectory(payload: dict, *, summary: dict | None = None) -> list[UsageRow]:
    trajectory = payload.get("trajectory") if isinstance(payload.get("trajectory"), dict) else payload
    if not isinstance(trajectory, dict):
        return []
    cascade_id = str(trajectory.get("cascadeId") or trajectory.get("trajectoryId") or "")
    if not cascade_id:
        return []
    steps = trajectory.get("steps") if isinstance(trajectory.get("steps"), list) else []
    project = _project_name(summary or {})
    fallback_time = parse_iso_time((summary or {}).get("lastModifiedTime"))

    execution_models: dict[str, str] = {}
    for executor in trajectory.get("executorMetadatas") or []:
        if not isinstance(executor, dict) or not executor.get("executionId"):
            continue
        cascade_config = executor.get("cascadeConfig")
        planner = cascade_config.get("plannerConfig") if isinstance(cascade_config, dict) else None
        model = planner.get("modelName") if isinstance(planner, dict) else None
        if model:
            execution_models[str(executor["executionId"])] = str(model)

    rows: list[UsageRow] = []
    for index, generator in enumerate(trajectory.get("generatorMetadata") or []):
        if not isinstance(generator, dict):
            continue
        chat = generator.get("chatModel")
        usage = chat.get("usage") if isinstance(chat, dict) else None
        if not isinstance(usage, dict):
            continue
        fresh_input = _count(usage.get("inputTokens"))
        cache_read = _count(usage.get("cacheReadTokens"))
        output = _count(usage.get("outputTokens"))
        if fresh_input + cache_read + output <= 0:
            continue

        occurred_at = None
        step_indices = generator.get("stepIndices") if isinstance(generator.get("stepIndices"), list) else []
        for step_index in step_indices:
            try:
                step = steps[int(step_index)]
            except (ValueError, TypeError, IndexError):
                continue
            metadata = step.get("metadata") if isinstance(step, dict) else None
            if isinstance(metadata, dict):
                occurred_at = (
                    parse_iso_time(metadata.get("completedAt"))
                    or parse_iso_time(metadata.get("finishedGeneratingAt"))
                    or parse_iso_time(metadata.get("createdAt"))
                )
            if occurred_at is not None:
                break
        occurred_at = occurred_at or fallback_time
        if occurred_at is None:
            continue

        execution_id = str(generator.get("executionId") or "")
        model = execution_models.get(execution_id) or chat.get("responseModel") or usage.get("model")
        response_id = usage.get("responseId") or usage.get("messageId")
        raw_id = (
            stable_id("antigravity-local", response_id)
            if response_id
            else stable_id("antigravity-local", cascade_id, execution_id, index, step_indices)
        )
        rows.append(
            UsageRow(
                source=SOURCE,
                tool_key=TOOL_KEY,
                model_key=canonical_model(model),
                occurred_at=occurred_at.astimezone(UTC),
                session_id=cascade_id,
                project=project,
                # These are separate additive fields: cacheReadTokens can be
                # greater than inputTokens. Normalized input is total input.
                input_tokens=fresh_input + cache_read,
                cached_input_tokens=cache_read,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=output,
                # thinkingOutputTokens is a subset of outputTokens.
                reasoning_tokens=(
                    _count(usage.get("thinkingOutputTokens"))
                    if usage.get("thinkingOutputTokens") is not None
                    else None
                ),
                cost_usd=None,
                raw_id=raw_id,
            )
        )
    return rows


def ingest(*, database_path: Path, pricing: PricingEngine) -> dict:
    try:
        base_url, csrf_token = _antigravity_local_connection()
    except RuntimeError:
        return skipped_result(
            database_path=database_path,
            source=SOURCE,
            reason="Experimental Antigravity localhost gRPC-Web: desktop is not running or the RPC is unavailable.",
        )
    usage_rows: list[UsageRow] = []
    pending_signatures: list[tuple[tuple[str, str], tuple[object, ...]]] = []
    try:
        _assert_allowed_https_host(base_url, ANTIGRAVITY_RPC_ALLOWED_HOSTS)
        with httpx.Client(verify=False, timeout=15, trust_env=False, follow_redirects=False) as client:
            summaries_payload = _rpc_json(
                client,
                base_url=base_url,
                csrf_token=csrf_token,
                method="GetAllCascadeTrajectories",
                payload={},
            )
            raw_summaries = summaries_payload.get("trajectorySummaries") or {}
            summaries = raw_summaries.items() if isinstance(raw_summaries, dict) else (
                (str(item.get("trajectoryId") or index), item)
                for index, item in enumerate(raw_summaries)
                if isinstance(item, dict)
            )
            seen = 0
            fetched = 0
            for cascade_id, summary in summaries:
                if not isinstance(summary, dict):
                    continue
                seen += 1
                cascade_id = str(cascade_id)
                signature = (
                    summary.get("trajectoryId"),
                    summary.get("lastModifiedTime"),
                    summary.get("stepCount"),
                    summary.get("status"),
                )
                state_key = (base_url, cascade_id)
                if _TRAJECTORY_SIGNATURES.get(state_key) == signature:
                    continue
                trajectory = _rpc_json(
                    client,
                    base_url=base_url,
                    csrf_token=csrf_token,
                    method="GetCascadeTrajectory",
                    payload={"cascadeId": cascade_id, "trajectoryVerbosity": PROD_UI_VERBOSITY},
                )
                fetched += 1
                usage_rows.extend(parse_trajectory(trajectory, summary=summary))
                pending_signatures.append((state_key, signature))
    except (httpx.HTTPError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return failed_result(
            database_path=database_path,
            source=SOURCE,
            reason=f"Experimental Antigravity localhost gRPC-Web usage lookup failed ({type(exc).__name__}).",
        )

    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=usage_rows,
    )
    for state_key, signature in pending_signatures:
        _TRAJECTORY_SIGNATURES[state_key] = signature
    return {**result, "trajectoriesSeen": seen, "trajectoriesFetched": fetched}
