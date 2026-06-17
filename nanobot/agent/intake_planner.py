"""Structured preflight planner for interactive intake prompts."""

from __future__ import annotations

import json
from typing import Any, Mapping

import json_repair

from nanobot.providers.base import LLMProvider
from nanobot.webui.interactive_prompt import normalize_interactive_prompt

_SYSTEM_PROMPT = """You are an intake planner for a chat assistant UI.

Your job is to decide whether the assistant should ask the user a structured interactive prompt card
before doing the main task.

Return JSON only with this shape:
{
  "should_prompt": boolean,
  "reason": string,
  "prompt": {
    "title": string?,
    "question": string,
    "options": [{"id": string, "label": string, "description": string?}],
    "questions": [{
      "id": string,
      "question": string,
      "options": [{"id": string, "label": string, "description": string?}],
      "allowFreeform": boolean?
    }]?,
    "allowFreeform": boolean?,
    "allowSkip": boolean?,
    "stepIndex": integer?,
    "totalSteps": integer?,
    "promptMessage": string?
  } | null
}

Rules:
- Prompt only when required information is missing and the assistant cannot safely continue without it.
- Prefer prompting when the user explicitly invited clarifying questions.
- Do not prompt for open-ended niceties, optional polish, or information that can be safely inferred.
- Ask one card with at most two questions. First evaluate all missing required information, then greedily choose the one or two most blocking independent questions for prompt.questions.
- If two independent blocking questions are not available, ask only the single most blocking next question.
- Never split two independent initial questions into consecutive cards.
- A second interactive prompt round is allowed only when the next question genuinely depends on the user's previous interactive-prompt answer.
- Do not plan more than two interactive prompt rounds for one session/task; after two rounds, remaining clarification should be asked as normal text.
- Keep options compact and mutually distinct.
- Do not include Other, Something else, Custom, or equivalent fallback choices in options; the UI always provides freeform input for every question.
- Use options only for concrete meaningful choices.
- If enough information is already available, return should_prompt=false.
"""


def _history_preview(history: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, str]]:
    preview: list[dict[str, str]] = []
    for message in history[-limit:]:
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        if len(text) > 500:
            text = text[:500].rstrip() + "..."
        preview.append({"role": str(role), "content": text})
    return preview


async def plan_interactive_prompt(
    *,
    provider: LLMProvider,
    model: str,
    history: list[dict[str, Any]],
    user_message: str,
) -> dict[str, Any] | None:
    """Return a normalized interactive prompt plan or ``None`` when no prompt is needed."""
    payload = {
        "history": _history_preview(history),
        "current_user_message": user_message,
    }
    response = await provider.chat_with_retry(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        tools=None,
        model=model,
        max_tokens=500,
        temperature=0,
        tool_choice="none",
    )
    if response.finish_reason == "error" or not isinstance(response.content, str) or not response.content.strip():
        return None
    try:
        raw = json_repair.loads(response.content)
    except Exception:
        try:
            raw = json.loads(response.content)
        except Exception:
            return None
    if not isinstance(raw, Mapping) or raw.get("should_prompt") is not True:
        return None
    prompt_raw = raw.get("prompt")
    if not isinstance(prompt_raw, Mapping):
        return None
    normalized = normalize_interactive_prompt({
        "promptId": "planner:pending",
        "title": prompt_raw.get("title"),
        "question": prompt_raw.get("question"),
        "options": prompt_raw.get("options"),
        "questions": prompt_raw.get("questions"),
        "allowFreeform": prompt_raw.get("allowFreeform"),
        "allowSkip": prompt_raw.get("allowSkip"),
        "stepIndex": prompt_raw.get("stepIndex"),
        "totalSteps": prompt_raw.get("totalSteps"),
        "status": "pending",
    })
    if normalized is None:
        return None
    out = {
        "title": normalized.get("title"),
        "question": normalized["question"],
        "options": normalized["options"],
        "questions": normalized.get("questions"),
        "allow_freeform": bool(normalized.get("allowFreeform", False)),
        "allow_skip": bool(normalized.get("allowSkip", False)),
    }
    if isinstance(normalized.get("stepIndex"), int):
        out["step_index"] = normalized["stepIndex"]
    if isinstance(normalized.get("totalSteps"), int):
        out["total_steps"] = normalized["totalSteps"]
    prompt_message = prompt_raw.get("promptMessage")
    if isinstance(prompt_message, str) and prompt_message.strip():
        out["prompt_message"] = prompt_message.strip()
    return out
