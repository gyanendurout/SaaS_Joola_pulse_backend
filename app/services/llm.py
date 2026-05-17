"""OpenAI client wrapper. Returns strict-JSON only."""
from __future__ import annotations

import json
from typing import Any

import structlog
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

log = structlog.get_logger()


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=get_settings().openai_api_key)


@retry(reraise=True, stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=8))
async def chat_json(
    *,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Send a chat completion that must return a valid JSON object."""
    settings = get_settings()
    use_model = model or settings.openai_model_smart

    resp = await _client().chat.completions.create(
        model=use_model,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = resp.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.warning("llm_json_decode_failed", error=str(e), preview=content[:200])
        # One retry round-trip with a corrective prompt.
        retry_resp = await _client().chat.completions.create(
            model=use_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        f"Your response failed JSON parsing: {e}. "
                        "Reply with valid JSON only — no prose, no markdown."
                    ),
                },
            ],
        )
        retry_content = retry_resp.choices[0].message.content or "{}"
        return json.loads(retry_content)
