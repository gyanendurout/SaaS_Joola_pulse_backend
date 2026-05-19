"""Generator orchestrator — assembles signals, builds prompt, streams from OpenAI,
parses + critiques + persists, and emits SSE-friendly event dicts.

Used by:
- `routes/content.py` POST /generate/stream (StreamingResponse wraps it)
- `routes/content.py` POST /generate (non-streaming wrapper consumes the same iterator)
- `routes/content.py` POST /drafts/{id}/regenerate
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog
from openai import AsyncOpenAI

from app.config import get_settings
from app.content.parsers import parse_output, validate_output
from app.content.prompt_builder import build_prompt
from app.content.rate_limiter import get_limiter
from app.content.signal_collectors import assemble_bundle
from app.content.types import (
    ContextBundle,
    ContentType,
    CriticResult,
    GenerateRequest,
)
from app.db import service_client

log = structlog.get_logger()


# ─── Cost / model config ──────────────────────────────────────────────────────

# Approx $/1M tokens for gpt-4o-mini per OpenAI public pricing
_PRICE_PER_1M = {
    "gpt-4o-mini": {"in": 0.15, "out": 0.60},
    "gpt-4o":      {"in": 5.00, "out": 15.00},
}


def _model_for(content_type: str) -> str:
    """Default model per format per spec §12."""
    # Blog/IG/Twitter all default to gpt-4o-mini in v1
    return "gpt-4o-mini"


def _variant_count(content_type: str, tone: str) -> int:
    """Per spec §11 — Blog 1 / IG 3 / Twitter 2 (always 2 if defensive)."""
    if content_type == "blog":
        return 1
    if content_type == "ig_post":
        return 3
    if content_type == "twitter_response":
        return 2 if tone == "defensive" else 2  # spec: always 2 on Crisis, otherwise still 2
    return 1


def _max_tokens(content_type: str) -> int:
    return {
        "blog": 1500,
        "ig_post": 400,
        "twitter_response": 200,
    }.get(content_type, 800)


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p = _PRICE_PER_1M.get(model, _PRICE_PER_1M["gpt-4o-mini"])
    return round(
        (prompt_tokens / 1_000_000.0) * p["in"]
        + (completion_tokens / 1_000_000.0) * p["out"],
        5,
    )


# ─── Event helpers ────────────────────────────────────────────────────────────

def _event(type_: str, **payload: Any) -> dict[str, Any]:
    return {"type": type_, **payload}


def _sse_bytes(evt: dict[str, Any]) -> bytes:
    """Encode an event dict as an SSE frame: `data: {json}\n\n`."""
    return f"data: {json.dumps(evt, default=str)}\n\n".encode("utf-8")


# ─── Persistence ──────────────────────────────────────────────────────────────

def _persist_run(row: dict[str, Any]) -> None:
    try:
        service_client().table("content_generation_runs").insert(row).execute()
    except Exception as e:
        log.warning("persist_run_failed", error=str(e))


def _persist_draft(row: dict[str, Any]) -> str | None:
    try:
        res = service_client().table("content_drafts").insert(row).execute()
        data = (res.data or [{}])[0]
        return data.get("id")
    except Exception as e:
        log.warning("persist_draft_failed", error=str(e))
        return None


# ─── Main streaming orchestrator ──────────────────────────────────────────────

async def generate_stream(
    request: GenerateRequest,
) -> AsyncIterator[dict[str, Any]]:
    """Yields event dicts (not bytes). Caller wraps with `_sse_bytes` for HTTP.

    Event types: `meta`, `token`, `parsed`, `critic`, `done`, `error`.
    """
    settings = get_settings()
    started_at = time.time()
    user_key = request.created_by or "anon@joola.com"
    content_type = (
        request.content_type.value if hasattr(request.content_type, "value")
        else str(request.content_type)
    )
    tone = request.tone.value if hasattr(request.tone, "value") else str(request.tone)
    model = _model_for(content_type)
    run_id = str(uuid.uuid4())
    bundle: ContextBundle | None = None
    accumulated = ""
    prompt_tokens = 0
    completion_tokens = 0
    cost_usd = 0.0

    try:
        # 1. rate-limit
        await get_limiter().check(user_key)

        # 2. assemble signals
        bundle = await assemble_bundle(
            request.signals_config,
            str(request.source_article_id) if request.source_article_id else None,
        )

        # 3. build prompt
        prompt = build_prompt(content_type, bundle, request)
        prompt_hash = hashlib.sha256(
            (prompt["system"] + "|" + prompt["user"]).encode("utf-8")
        ).hexdigest()[:16]

        # 4. meta event
        yield _event(
            "meta",
            run_id=run_id,
            content_type=content_type,
            model=model,
            variant_count=_variant_count(content_type, tone),
        )

        if not settings.openai_api_key:
            raise RuntimeError("OpenAI API key not configured (OPENAI_API_KEY)")

        client = AsyncOpenAI(api_key=settings.openai_api_key)

        # 5. stream
        stream = await client.chat.completions.create(
            model=model,
            temperature=0.7,
            max_tokens=_max_tokens(content_type),
            stream=True,
            messages=[
                {"role": "system", "content": prompt["system"]},
                {"role": "user", "content": prompt["user"]},
            ],
        )
        async for chunk in stream:
            try:
                delta = chunk.choices[0].delta.content if chunk.choices else None
            except Exception:
                delta = None
            if delta:
                accumulated += delta
                yield _event("token", text=delta)

        # 6. parse
        parsed = parse_output(content_type, accumulated)
        crisis = (tone == "defensive")
        ok, reasons = validate_output(content_type, parsed, crisis=crisis)
        yield _event("parsed", parsed=parsed, valid=ok, reasons=reasons)

        # 7. critic pass — if fail, regenerate ONCE with violations injected
        from app.content.critic import critique

        critic_result: CriticResult = await critique(
            draft=accumulated,
            content_type=content_type,
            brand_voice=bundle.brand_voice,
            tone=tone,
        )

        if not critic_result.passed:
            # Regenerate once
            fix_msg = (
                "Your previous draft failed the rubric. Violations:\n- "
                + "\n- ".join(critic_result.violations or [])
                + f"\n\nSuggested fix: {critic_result.suggested_fix}\n\n"
                + "Regenerate the draft fixing every violation. Same output format as before."
            )
            try:
                retry = await client.chat.completions.create(
                    model=model,
                    temperature=0.6,
                    max_tokens=_max_tokens(content_type),
                    messages=[
                        {"role": "system", "content": prompt["system"]},
                        {"role": "user", "content": prompt["user"]},
                        {"role": "assistant", "content": accumulated},
                        {"role": "user", "content": fix_msg},
                    ],
                )
                retry_text = retry.choices[0].message.content or ""
                if retry_text:
                    accumulated = retry_text
                    parsed = parse_output(content_type, accumulated)
                    if retry.usage:
                        prompt_tokens += retry.usage.prompt_tokens or 0
                        completion_tokens += retry.usage.completion_tokens or 0
                    # Second critic pass (informational only — don't loop)
                    critic_result = await critique(
                        draft=accumulated,
                        content_type=content_type,
                        brand_voice=bundle.brand_voice,
                        tone=tone,
                    )
            except Exception as e:
                log.warning("regenerate_after_critic_failed", error=str(e))

        yield _event("critic", **critic_result.model_dump())

        # 8. token usage — note streaming responses don't always provide usage
        try:
            # OpenAI's streaming responses sometimes include usage in the final chunk;
            # if not, estimate by character count / 4 as a rough proxy.
            prompt_tokens = prompt_tokens or max(1, len(prompt["system"] + prompt["user"]) // 4)
            completion_tokens = completion_tokens or max(1, len(accumulated) // 4)
        except Exception:
            pass
        cost_usd = _estimate_cost(model, prompt_tokens, completion_tokens)
        await get_limiter().record_cost(cost_usd)

        # 9. persist run
        latency_ms = int((time.time() - started_at) * 1000)
        _persist_run({
            "id": run_id,
            "created_by": user_key,
            "content_type": content_type,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "status": "success",
            "input_signals": json.loads(request.signals_config.model_dump_json()),
            "prompt_hash": prompt_hash,
        })

        # 10. persist draft
        title = parsed.get("title") if content_type == "blog" else (
            parsed.get("hook") if content_type == "ig_post" else None
        )
        hashtags = parsed.get("hashtags") if content_type == "ig_post" else None
        snapshot = json.loads(bundle.model_dump_json())
        draft_id = _persist_draft({
            "created_by": user_key,
            "content_type": content_type,
            "status": "draft",
            "title": title,
            "body": accumulated,
            "hashtags": hashtags,
            "metadata": {
                "parsed": parsed,
                "valid": ok,
                "validation_reasons": reasons,
                "critic": critic_result.model_dump(),
                "tone": tone,
                "length": request.length.value if hasattr(request.length, "value") else str(request.length),
                "audience": request.audience.value if hasattr(request.audience, "value") else str(request.audience),
                "cta_goal": request.cta_goal.value if hasattr(request.cta_goal, "value") else str(request.cta_goal),
            },
            "source_article_id": str(request.source_article_id) if request.source_article_id else None,
            "source_signal_snapshot": snapshot,
            "generation_run_id": run_id,
            "version": 1,
        }) or str(uuid.uuid4())

        # 11. done
        yield _event(
            "done",
            draft_id=draft_id,
            run_id=run_id,
            body=accumulated,
            title=title,
            hashtags=hashtags or [],
            cost_usd=cost_usd,
            critic=critic_result.model_dump(),
        )

    except Exception as exc:
        log.exception("generate_stream_failed", error=str(exc))
        # Best-effort run row for telemetry
        try:
            _persist_run({
                "id": run_id,
                "created_by": user_key,
                "content_type": content_type,
                "model": model,
                "prompt_tokens": prompt_tokens or 0,
                "completion_tokens": completion_tokens or 0,
                "cost_usd": cost_usd,
                "latency_ms": int((time.time() - started_at) * 1000),
                "status": "error",
                "error_message": str(exc),
                "input_signals": json.loads(request.signals_config.model_dump_json())
                if request.signals_config else {},
            })
        except Exception:
            pass
        yield _event("error", message=str(exc))


async def generate_once(request: GenerateRequest) -> dict[str, Any]:
    """Non-streaming wrapper — consumes the iterator and returns the final payload.

    Returns one of:
      - {"ok": True, "draft_id": ..., "body": ..., ...}
      - {"ok": False, "error": str}
    """
    result: dict[str, Any] = {"ok": False}
    async for evt in generate_stream(request):
        et = evt.get("type")
        if et == "done":
            result = {
                "ok": True,
                "draft_id": evt["draft_id"],
                "run_id": evt["run_id"],
                "body": evt.get("body"),
                "title": evt.get("title"),
                "hashtags": evt.get("hashtags") or [],
                "cost_usd": evt.get("cost_usd", 0.0),
                "critic": evt.get("critic"),
            }
        elif et == "error":
            result = {"ok": False, "error": evt.get("message")}
    return result


async def sse_iterator(request: GenerateRequest) -> AsyncIterator[bytes]:
    """Bytes iterator suitable for StreamingResponse media_type='text/event-stream'."""
    async for evt in generate_stream(request):
        yield _sse_bytes(evt)
