"""Provider-backed explanation layer for SurgeGuard recovery plans."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from google import genai
from google.genai import types

from app.config import settings

logger = logging.getLogger(__name__)


SYSTEM_INSTRUCTION = """
You are the SurgeGuard Recovery Explainer.

Your role is ONLY to explain a recovery plan that SurgeGuard has already
computed. The backend scheduler and recovery engine are the operational
source of truth.

STRICT RULES:

1. Never invent numbers, costs, times, percentages, counts, risks,
   operational effects, or capabilities.
2. Never calculate or recompute SLA status, queue position, priority,
   predicted dispatch, recovery time, or facility risk.
3. Never choose a recovery plan for the user.
4. Never approve, reject, execute, or trigger a recovery action.
5. Never claim that a projection is a guarantee.
6. Clearly distinguish supplied current/baseline information from projected
   information and from assumptions.
7. If the supplied context does not contain information needed to answer,
   explicitly say that the information is not available in the supplied
   SurgeGuard context.
8. Explain trade-offs and side effects when they are present in the supplied
   plan.
9. If asked what happens if nothing is done, explain the supplied
   "do-nothing" plan rather than inventing an outcome.
10. If asked which plan is best, do not select one. Instead, explain the
    documented differences between the plans supplied in context.
11. Treat all values in the grounding context as authoritative application
    data, not as facts to reinterpret or replace.
12. Do not add general operational claims, mechanisms, benefits, or consequences
    unless they are explicitly supported by the supplied SurgeGuard context.
13. Keep the answer useful and understandable to an operations user.

The user is asking for an explanation, not a new operational decision.
"""


def _build_prompt(question: str, plan_context: dict[str, Any]) -> str:
    """Build a bounded prompt from backend-generated plan data."""

    return f"""
Answer the operator's question using ONLY the SurgeGuard context below.

OPERATOR QUESTION:
{question}

SURGEGUARD RECOVERY CONTEXT:
{json.dumps(plan_context, indent=2, default=str)}

Remember:
- Explain the supplied data.
- Do not create missing data.
- Do not make an operational decision.
- Do not approve or execute anything.
"""


async def _generate_with_gemini(prompt: str) -> str:
    """Generate an explanation using Google's Gemini API."""

    if not settings.gemini_api_key:
        raise RuntimeError("Gemini API key is not configured")

    client = genai.Client(api_key=settings.gemini_api_key)

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model=settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.2,
                ),
            ),
            timeout=settings.gemini_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Gemini request timed out") from exc

    answer = response.text

    if not answer or not answer.strip():
        raise RuntimeError("Gemini returned an empty explanation")

    return answer.strip()


async def _generate_with_openrouter(prompt: str) -> str:
    """Generate an explanation through OpenRouter."""

    if not settings.openrouter_api_key:
        raise RuntimeError("OpenRouter API key is not configured")

    payload = {
        "model": settings.openrouter_model,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_INSTRUCTION,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        "temperature": 0.1,
        "max_tokens": 768,
    }

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(
            timeout=settings.gemini_timeout_seconds,
        ) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise RuntimeError("OpenRouter request timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"OpenRouter request failed with HTTP {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("OpenRouter request failed") from exc

    data = response.json()

    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("OpenRouter returned an invalid response") from exc

    if not answer or not answer.strip():
        raise RuntimeError("OpenRouter returned an empty explanation")

    return answer.strip()


async def explain_recovery_plan(
    *,
    question: str,
    plan_context: dict[str, Any],
) -> str:
    """Generate a grounded recovery-plan explanation.

    Gemini is attempted first. OpenRouter is used when Gemini is unavailable.
    Neither provider is allowed to become the operational source of truth.
    """

    prompt = _build_prompt(question, plan_context)

    try:
        return await _generate_with_gemini(prompt)
    except Exception:
        logger.exception(
            "Direct Gemini explanation failed; trying OpenRouter fallback"
        )

    try:
        return await _generate_with_openrouter(prompt)
    except Exception:
        logger.exception("OpenRouter explanation fallback failed")
        raise RuntimeError(
            "Both Gemini and OpenRouter explanation services are unavailable"
        )
