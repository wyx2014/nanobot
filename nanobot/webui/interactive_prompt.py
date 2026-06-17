"""Interactive prompt schema helpers for WebUI chat sessions."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

OUTBOUND_META_INTERACTIVE_PROMPT = "_interactive_prompt"
INBOUND_META_INTERACTIVE_PROMPT_ANSWER = "interactive_prompt_answer"
SESSION_META_PENDING_INTERACTIVE_PROMPT = "interactive_prompt_pending"

_PROMPT_STATUS = {"pending", "answered", "skipped", "expired"}
_ANSWER_TYPES = {"option", "freeform", "skip", "group"}
_OTHER_OPTION_LABELS = {
    "other",
    "others",
    "something else",
    "another",
    "custom",
    "custom answer",
    "i'll explain",
    "i will explain",
    "something else (i'll explain)",
    "something else (i will explain)",
    "其他",
    "其它",
    "其他选项",
    "其它选项",
    "其他（我来说明）",
    "其他(我来说明)",
    "其他（我会说明）",
    "其他(我会说明)",
    "其他，我来说明",
    "别的",
    "自定义",
}
_ACTIVE_PROMPT_REQUESTED: ContextVar[bool] = ContextVar(
    "nanobot_active_interactive_prompt_requested",
    default=False,
)


class InteractivePromptRequested(RuntimeError):
    """Signal that the current turn emitted a persisted interactive prompt."""

    def __init__(self, prompt_id: str):
        super().__init__(f"interactive prompt requested: {prompt_id}")
        self.prompt_id = prompt_id


def is_other_like_option_label(label: str) -> bool:
    normalized = " ".join(label.strip().lower().replace("’", "'").split())
    return normalized in _OTHER_OPTION_LABELS


def normalize_interactive_prompt(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    prompt_id = raw.get("promptId")
    question = raw.get("question")
    options = raw.get("options")
    if not isinstance(prompt_id, str) or not prompt_id.strip():
        return None
    questions = raw.get("questions")
    has_group_questions = isinstance(questions, list) and bool(questions)
    if has_group_questions and len(questions) > 2:
        return None
    if not has_group_questions and (not isinstance(question, str) or not question.strip()):
        return None
    if not has_group_questions and (not isinstance(options, list) or not options):
        return None

    def normalize_options(raw_options: Any) -> tuple[list[dict[str, str]], set[str]] | None:
        if not isinstance(raw_options, list) or not raw_options:
            return None
        normalized_options: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for option in raw_options:
            if not isinstance(option, dict):
                return None
            option_id = option.get("id")
            label = option.get("label")
            if not isinstance(option_id, str) or not option_id.strip():
                return None
            if not isinstance(label, str) or not label.strip():
                return None
            if is_other_like_option_label(label):
                continue
            option_id = option_id.strip()
            if option_id in seen_ids:
                return None
            seen_ids.add(option_id)
            normalized_option: dict[str, str] = {
                "id": option_id,
                "label": label.strip(),
            }
            description = option.get("description")
            if isinstance(description, str) and description.strip():
                normalized_option["description"] = description.strip()
            normalized_options.append(normalized_option)
        if not normalized_options:
            return None
        return normalized_options, seen_ids

    normalized_options: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    if not has_group_questions:
        normalized_result = normalize_options(options)
        if normalized_result is None:
            return None
        normalized_options, seen_ids = normalized_result

    status = raw.get("status")
    normalized: dict[str, Any] = {
        "promptId": prompt_id.strip(),
        "question": question.strip() if isinstance(question, str) and question.strip() else "",
        "options": normalized_options,
        "status": status if isinstance(status, str) and status in _PROMPT_STATUS else "pending",
        "allowFreeform": True,
    }
    if has_group_questions:
        normalized_questions: list[dict[str, Any]] = []
        seen_question_ids: set[str] = set()
        for item in questions:
            if not isinstance(item, dict):
                return None
            question_id = item.get("id")
            question_text = item.get("question")
            if not isinstance(question_id, str) or not question_id.strip():
                return None
            if not isinstance(question_text, str) or not question_text.strip():
                return None
            question_id = question_id.strip()
            if question_id in seen_question_ids:
                return None
            seen_question_ids.add(question_id)
            option_result = normalize_options(item.get("options"))
            if option_result is None:
                return None
            item_options, item_option_ids = option_result
            normalized_question: dict[str, Any] = {
                "id": question_id,
                "question": question_text.strip(),
                "options": item_options,
                "allowFreeform": True,
            }
            if isinstance(item.get("allowFreeform"), bool):
                normalized_question["allowFreeform"] = item["allowFreeform"]
            answered_option_id = item.get("answeredOptionId")
            if isinstance(answered_option_id, str) and answered_option_id in item_option_ids:
                normalized_question["answeredOptionId"] = answered_option_id
            answered_text = item.get("answeredText")
            if isinstance(answered_text, str):
                normalized_question["answeredText"] = answered_text
            normalized_questions.append(normalized_question)
        normalized["questions"] = normalized_questions
    title = raw.get("title")
    if isinstance(title, str) and title.strip():
        normalized["title"] = title.strip()
    if isinstance(raw.get("allowFreeform"), bool):
        normalized["allowFreeform"] = raw["allowFreeform"]
    if isinstance(raw.get("allowSkip"), bool):
        normalized["allowSkip"] = raw["allowSkip"]
    if isinstance(raw.get("stepIndex"), int):
        normalized["stepIndex"] = raw["stepIndex"]
    if isinstance(raw.get("totalSteps"), int):
        normalized["totalSteps"] = raw["totalSteps"]
    answered_option_id = raw.get("answeredOptionId")
    if isinstance(answered_option_id, str) and answered_option_id in seen_ids:
        normalized["answeredOptionId"] = answered_option_id
    answered_text = raw.get("answeredText")
    if isinstance(answered_text, str):
        normalized["answeredText"] = answered_text
    return normalized


def normalize_interactive_prompt_answer(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    prompt_id = raw.get("promptId")
    answer_type = raw.get("answerType")
    if not isinstance(prompt_id, str) or not prompt_id.strip():
        return None
    if not isinstance(answer_type, str) or answer_type not in _ANSWER_TYPES:
        return None
    normalized: dict[str, Any] = {
        "promptId": prompt_id.strip(),
        "answerType": answer_type,
    }
    option_id = raw.get("optionId")
    if isinstance(option_id, str) and option_id.strip():
        normalized["optionId"] = option_id.strip()
    answers = raw.get("answers")
    if answer_type == "group":
        if not isinstance(answers, list) or not answers:
            return None
        normalized_answers: list[dict[str, Any]] = []
        for item in answers:
            if not isinstance(item, dict):
                return None
            question_id = item.get("questionId")
            item_answer_type = item.get("answerType")
            text = item.get("text")
            if not isinstance(question_id, str) or not question_id.strip():
                return None
            if item_answer_type not in {"option", "freeform"}:
                return None
            if not isinstance(text, str) or not text.strip():
                return None
            normalized_item: dict[str, Any] = {
                "questionId": question_id.strip(),
                "answerType": item_answer_type,
                "text": text.strip(),
            }
            item_option_id = item.get("optionId")
            if isinstance(item_option_id, str) and item_option_id.strip():
                normalized_item["optionId"] = item_option_id.strip()
            normalized_answers.append(normalized_item)
        normalized["answers"] = normalized_answers
    return normalized


def interactive_prompt_answer_session_extra(metadata: dict[str, Any] | None) -> dict[str, Any]:
    answer = normalize_interactive_prompt_answer(
        metadata.get(INBOUND_META_INTERACTIVE_PROMPT_ANSWER) if isinstance(metadata, dict) else None
    )
    return {INBOUND_META_INTERACTIVE_PROMPT_ANSWER: answer} if answer else {}


def set_interactive_prompt_requested(active: bool):
    return _ACTIVE_PROMPT_REQUESTED.set(active)


def reset_interactive_prompt_requested(token) -> None:
    _ACTIVE_PROMPT_REQUESTED.reset(token)


def interactive_prompt_requested_in_turn() -> bool:
    return _ACTIVE_PROMPT_REQUESTED.get()
