import json

from nanobot.providers.base import LLMProvider


def test_provider_serializes_bare_tool_content_dict():
    messages = [{
        "role": "tool",
        "tool_call_id": "call_pdf",
        "name": "create_pdf",
        "content": {"text": "created", "files": [{"path": "/tmp/report.pdf"}]},
    }]

    sanitized = LLMProvider._sanitize_empty_content(messages)

    assert isinstance(sanitized[0]["content"], str)
    assert json.loads(sanitized[0]["content"])["files"][0]["path"] == "/tmp/report.pdf"


def test_provider_keeps_typed_user_content_dict_as_block_list():
    messages = [{
        "role": "user",
        "content": {"type": "text", "text": "hello"},
    }]

    sanitized = LLMProvider._sanitize_empty_content(messages)

    assert sanitized[0]["content"] == [{"type": "text", "text": "hello"}]


def test_provider_preserves_typed_tool_content_blocks():
    messages = [{
        "role": "tool",
        "tool_call_id": "call_image",
        "name": "image",
        "content": [{"type": "text", "text": "created"}],
    }]

    sanitized = LLMProvider._sanitize_empty_content(messages)

    assert sanitized[0]["content"] == [{"type": "text", "text": "created"}]
