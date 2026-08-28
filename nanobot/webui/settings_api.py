"""Settings REST helpers for the WebUI HTTP surface.

The WebSocket channel owns transport/authentication. This module owns the
settings payload shape and the allowlisted config mutations exposed to WebUI.
"""

from __future__ import annotations

import os
import re
import time
from contextlib import suppress
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from nanobot import __version__
from nanobot.audio.streaming_transcription import (
    resolve_streaming_transcription_profile,
)
from nanobot.audio.transcription import resolve_transcription_config
from nanobot.audio.transcription_registry import (
    resolve_transcription_provider,
    transcription_provider_names,
)
from nanobot.config.loader import get_config_path, load_config, save_config
from nanobot.config.schema import (
    ModelCapability,
    ModelCapabilitySource,
    ModelPresetConfig,
    ProviderConfig,
)
from nanobot.providers.image_generation import (
    get_image_gen_provider,
    image_gen_provider_names,
)
from nanobot.providers.registry import PROVIDERS, create_dynamic_spec, find_by_name
from nanobot.security.workspace_access import workspace_sandbox_status
from nanobot.webui.token_usage import token_usage_payload
from nanobot.webui.workspaces import (
    read_webui_default_access_mode,
    write_webui_default_access_mode,
)

QueryParams = dict[str, list[str]]
RuntimeSurface = Literal["browser", "native"]
MODEL_CAPABILITIES: tuple[ModelCapability, ...] = (
    "text",
    "speech_to_text",
    "text_to_speech",
)
MODEL_CAPABILITY_SOURCES: tuple[ModelCapabilitySource, ...] = (
    "manual",
    "provider",
    "heuristic",
)
_EXCLUDED_DISCOVERED_MODEL_IDS = frozenset({"deepseek-r1"})
_STORED_MODEL_CAPABILITIES: tuple[ModelCapability, ...] = (
    *MODEL_CAPABILITIES,
    "text_to_speech",
    "vision",
    "image_generation",
)
_REMOVED_MODEL_CAPABILITIES = frozenset(
    {"vision", "image_generation"}
)

_OFFICIAL_PROVIDER_BY_API_HOST = {
    "api.stepfun.com": "stepfun",
    "api.stepfun.ai": "stepfun",
}


def _version_payload() -> dict[str, Any]:
    """Return version info for the settings payload."""
    return {
        "current": __version__,
    }

_RUNTIME_CAPABILITIES = {
    "can_restart_engine": False,
    "can_pick_folder": False,
    "can_open_logs": False,
    "can_export_diagnostics": False,
}

_NATIVE_RUNTIME_CAPABILITIES = {
    **_RUNTIME_CAPABILITIES,
    "can_restart_engine": True,
    "can_pick_folder": True,
    "can_open_logs": True,
    "can_export_diagnostics": True,
}

_BROWSER_RESTART_BEHAVIOR_BY_SECTION = {
    "appearance": "none",
    "models": "none",
    "providers": "none",
    "runtime": "engineRestart",
    "browser": "engineRestart",
    "image": "engineRestart",
    "apps": "engineRestart",
    "advanced": "appRestart",
}

_NATIVE_RESTART_BEHAVIOR_BY_SECTION = {
    **_BROWSER_RESTART_BEHAVIOR_BY_SECTION,
    "runtime": "engineRestart",
    "browser": "engineRestart",
    "image": "engineRestart",
    "apps": "engineRestart",
}

_WEB_SEARCH_PROVIDER_OPTIONS: tuple[dict[str, str], ...] = (
    {"name": "duckduckgo", "label": "DuckDuckGo", "credential": "none"},
    {"name": "brave", "label": "Brave Search", "credential": "api_key"},
    {"name": "tavily", "label": "Tavily", "credential": "api_key"},
    {"name": "searxng", "label": "SearXNG", "credential": "base_url"},
    {"name": "jina", "label": "Jina", "credential": "api_key"},
    {"name": "kagi", "label": "Kagi", "credential": "api_key"},
    {"name": "exa", "label": "Exa", "credential": "api_key"},
    {"name": "olostep", "label": "Olostep", "credential": "api_key"},
    {"name": "bocha", "label": "Bocha", "credential": "api_key"},
    {"name": "volcengine", "label": "Volcengine Search", "credential": "api_key"},
    {"name": "keenable", "label": "Keenable", "credential": "optional_api_key"},
)
_WEB_SEARCH_PROVIDER_BY_NAME = {
    provider["name"]: provider for provider in _WEB_SEARCH_PROVIDER_OPTIONS
}

_IMAGE_GENERATION_ASPECT_RATIOS = {
    "1:1",
    "3:4",
    "9:16",
    "4:3",
    "16:9",
    "3:2",
    "2:3",
    "21:9",
}
_CONTEXT_WINDOW_TOKEN_OPTIONS = {65_536, 200_000, 262_144}
_MODEL_CONFIGURATION_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_MODEL_LIST_UNSUPPORTED_BACKENDS = {
    "anthropic",
    "azure_openai",
    "bedrock",
    "github_copilot",
    "openai_codex",
}

_MODEL_LIST_CATALOG_PROVIDERS = {
    "aihubmix",
    "byteplus",
    "byteplus_coding_plan",
    "huggingface",
    "novita",
    "openrouter",
    "siliconflow",
    "volcengine",
    "volcengine_coding_plan",
}

_MODEL_LIST_OFFICIAL_PROVIDERS = {
    "ant_ling",
    "dashscope",
    "deepseek",
    "gemini",
    "groq",
    "longcat",
    "minimax",
    "minimax_anthropic",
    "mistral",
    "moonshot",
    "nvidia",
    "openai",
    "qianfan",
    "skywork",
    "stepfun",
    "xiaomi_mimo",
    "zhipu",
}


class WebUISettingsError(ValueError):
    """User-facing settings validation failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _normalize_surface(surface: str | None) -> RuntimeSurface:
    return "native" if surface in {"native", "desktop"} else "browser"


def runtime_capabilities(
    surface: str | None = "browser",
    overrides: dict[str, Any] | None = None,
) -> dict[str, bool]:
    """Return the capability flags exposed to the WebUI runtime."""
    base = (
        _NATIVE_RUNTIME_CAPABILITIES
        if _normalize_surface(surface) == "native"
        else _RUNTIME_CAPABILITIES
    )
    result = dict(base)
    for key, value in (overrides or {}).items():
        if key in result:
            result[key] = bool(value)
    return result


def restart_behavior_by_section(surface: str | None = "browser") -> dict[str, str]:
    return dict(
        _NATIVE_RESTART_BEHAVIOR_BY_SECTION
        if _normalize_surface(surface) == "native"
        else _BROWSER_RESTART_BEHAVIOR_BY_SECTION
    )


def decorate_settings_payload(
    payload: dict[str, Any],
    *,
    surface: str | None = "browser",
    runtime_capability_overrides: dict[str, Any] | None = None,
    restart_required_sections: list[str] | None = None,
    apply_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach runtime-surface metadata without changing the core settings shape."""
    surface_value = _normalize_surface(surface)
    sections = restart_required_sections
    if sections is None:
        raw_sections = payload.get("restart_required_sections") or []
        sections = [str(section) for section in raw_sections if isinstance(section, str)]
    sections = sorted(dict.fromkeys(sections))
    result = dict(payload)
    result["surface"] = surface_value
    result["runtime_surface"] = surface_value
    result["runtime_capabilities"] = runtime_capabilities(
        surface_value,
        runtime_capability_overrides,
    )
    result["restart_behavior_by_section"] = restart_behavior_by_section(surface_value)
    result["restart_required_sections"] = sections
    if sections:
        result["requires_restart"] = True
    else:
        result["requires_restart"] = bool(result.get("requires_restart", False))
    result["apply_state"] = apply_state or {
        "status": "pending" if result["requires_restart"] else "idle",
        "sections": sections,
    }
    return result


def _query_first(query: QueryParams, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _query_first_alias(query: QueryParams, snake: str, camel: str) -> str | None:
    value = _query_first(query, snake)
    return _query_first(query, camel) if value is None else value


def _mask_secret_hint(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:4]}••••{secret[-4:]}"


def _resolve_env_placeholders(value: str | None) -> str | None:
    if not value:
        return None
    missing = False

    def replace(match: re.Match[str]) -> str:
        nonlocal missing
        env_value = os.environ.get(match.group(1))
        if env_value is None:
            missing = True
            return ""
        return env_value

    resolved = _ENV_REF_RE.sub(replace, value).strip()
    if missing and not resolved:
        return None
    return resolved or None


def _provider_requires_api_key(spec: Any) -> bool:
    if spec.name == "azure_openai":
        return False
    if spec.is_oauth:
        return False
    if spec.is_local or spec.is_direct:
        return False
    return True


def _provider_requires_api_base(spec: Any) -> bool:
    if spec.name == "azure_openai":
        return True
    return bool(spec.backend == "openai_compat" and spec.is_direct and not spec.default_api_base)


def _oauth_provider_status(spec: Any) -> dict[str, Any]:
    if not getattr(spec, "is_oauth", False):
        return {"configured": False, "account": None, "expires_at": None, "login_supported": False}

    if spec.name == "openai_codex":
        try:
            from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
            from oauth_cli_kit.storage import FileTokenStorage
        except Exception:
            return {
                "configured": False,
                "account": None,
                "expires_at": None,
                "login_supported": False,
            }
        token = None
        with suppress(Exception):
            token = FileTokenStorage(
                token_filename=OPENAI_CODEX_PROVIDER.token_filename,
            ).load()
        expires_at = getattr(token, "expires", None) if token else None
        now_ms = int(time.time() * 1000)
        return {
            "configured": bool(
                token
                and token.access
                and (getattr(token, "refresh", None) or (expires_at and expires_at > now_ms))
            ),
            "account": getattr(token, "account_id", None) if token else None,
            "expires_at": expires_at,
            "login_supported": True,
        }

    if spec.name == "github_copilot":
        try:
            from nanobot.providers.github_copilot_provider import get_github_copilot_login_status
        except Exception:
            return {
                "configured": False,
                "account": None,
                "expires_at": None,
                "login_supported": False,
            }
        token = None
        with suppress(Exception):
            token = get_github_copilot_login_status()
        return {
            "configured": bool(token and token.access and token.expires > int(time.time() * 1000)),
            "account": getattr(token, "account_id", None) if token else None,
            "expires_at": getattr(token, "expires", None) if token else None,
            "login_supported": True,
        }

    return {"configured": False, "account": None, "expires_at": None, "login_supported": False}


def _provider_configured_for_settings(spec: Any, provider_config: Any) -> bool:
    if spec.is_oauth:
        return bool(_oauth_provider_status(spec)["configured"])
    if _provider_requires_api_base(spec):
        return bool(provider_config.api_base)
    if _provider_requires_api_key(spec):
        return bool(provider_config.api_key)
    return bool(
        provider_config.api_key
        or provider_config.api_base
        or getattr(provider_config, "region", None)
        or getattr(provider_config, "profile", None)
    )


def _dynamic_provider_items(config: Any) -> list[tuple[str, ProviderConfig]]:
    return [
        (name, provider_config)
        for name, provider_config in (config.providers.model_extra or {}).items()
        if isinstance(provider_config, ProviderConfig)
    ]


def _canonical_provider_name_for_api_base(api_base: str | None) -> str | None:
    """Map an official API host to the provider id understood by runtimes.

    Display names are user-editable and therefore cannot safely double as
    runtime provider ids.  Keep this allowlist deliberately small: some
    vendors expose multiple incompatible products from the same host.
    """
    raw = (api_base or "").strip()
    if not raw:
        return None
    try:
        hostname = (urlsplit(raw).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return _OFFICIAL_PROVIDER_BY_API_HOST.get(hostname)


def _provider_has_explicit_settings(provider_config: ProviderConfig) -> bool:
    return bool(
        provider_config.api_key
        or provider_config.api_base
        or provider_config.api_type != "auto"
        or provider_config.extra_headers
        or provider_config.extra_body
        or provider_config.extra_query
    )


def normalize_official_dynamic_providers(config: Any) -> bool:
    """Migrate custom aliases of official endpoints to canonical providers.

    In particular, a user may call the StepFun service simply ``step``.  That
    works for OpenAI-compatible chat, but speech adapters resolve the canonical
    id ``stepfun``.  Rewriting the id once keeps chat, ASR and settings aligned.
    """
    changed = False
    extra_providers = config.providers.model_extra or {}
    for source_name, source_config in list(_dynamic_provider_items(config)):
        canonical_name = _canonical_provider_name_for_api_base(source_config.api_base)
        if not canonical_name or canonical_name == source_name:
            continue
        target_config = getattr(config.providers, canonical_name, None)
        if not isinstance(target_config, ProviderConfig):
            continue
        # Never overwrite a separately configured canonical account.
        if _provider_has_explicit_settings(target_config):
            continue

        for field in (
            "label",
            "api_key",
            "api_base",
            "extra_headers",
            "extra_body",
            "extra_query",
        ):
            setattr(target_config, field, getattr(source_config, field))
        # Built-in providers select their protocol from the registry; the
        # custom-provider-only api_type override cannot be carried across.
        target_config.api_type = "auto"

        def matches_source(value: str | None) -> bool:
            return bool(
                value
                and value.replace("-", "_") == source_name.replace("-", "_")
            )

        for preset in config.model_presets.values():
            if matches_source(preset.provider):
                preset.provider = canonical_name
        defaults = config.agents.defaults
        if matches_source(defaults.provider):
            defaults.provider = canonical_name
        for fallback in defaults.fallback_models:
            if not isinstance(fallback, str) and matches_source(fallback.provider):
                fallback.provider = canonical_name
        if matches_source(config.transcription.provider):
            config.transcription.provider = canonical_name
        if matches_source(config.tools.image_generation.provider):
            config.tools.image_generation.provider = canonical_name

        del extra_providers[source_name]
        changed = True
    return changed


def _resolve_settings_provider(
    config: Any,
    provider_name: str,
) -> tuple[Any, str, ProviderConfig] | None:
    spec = find_by_name(provider_name)
    if spec is not None:
        provider_config = getattr(config.providers, spec.name, None)
        if isinstance(provider_config, ProviderConfig):
            return spec, spec.name, provider_config
        return None

    normalized = provider_name.replace("-", "_")
    for extra_name, provider_config in _dynamic_provider_items(config):
        if provider_name == extra_name or normalized == extra_name.replace("-", "_"):
            return create_dynamic_spec(extra_name), extra_name, provider_config
    return None


def _provider_settings_row(
    name: str,
    spec: Any,
    provider_config: ProviderConfig,
    *,
    custom: bool = False,
) -> dict[str, Any]:
    oauth_status = _oauth_provider_status(spec) if spec.is_oauth else None
    row = {
        "name": name,
        "label": provider_config.label or spec.label,
        "configured": (
            bool(oauth_status["configured"])
            if oauth_status is not None
            else _provider_configured_for_settings(spec, provider_config)
        ),
        "auth_type": "oauth" if spec.is_oauth else "api_key",
        "api_key_required": _provider_requires_api_key(spec),
        "api_key_hint": _mask_secret_hint(provider_config.api_key),
        "api_base": provider_config.api_base,
        "default_api_base": spec.default_api_base or None,
        "model_selectable": not spec.is_transcription_only,
        "custom": custom,
    }
    if oauth_status is not None:
        row["oauth_account"] = oauth_status["account"]
        row["oauth_expires_at"] = oauth_status["expires_at"]
        row["oauth_login_supported"] = oauth_status["login_supported"]
    if spec.name == "openai":
        row["api_type"] = provider_config.api_type
    return row


def _model_catalog_kind(spec: Any) -> str:
    if spec.name in _MODEL_LIST_CATALOG_PROVIDERS:
        return "catalog"
    if spec.name in _MODEL_LIST_OFFICIAL_PROVIDERS:
        return "official"
    if spec.is_local:
        return "local"
    if spec.is_direct:
        return "custom"
    if spec.is_gateway:
        return "catalog"
    return "official"


def _model_id_from_row(row: Any) -> str | None:
    if isinstance(row, str):
        return row.strip() or None
    if not isinstance(row, dict):
        return None
    for key in ("id", "name", "model"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _model_context_window(row: Any) -> int | None:
    if not isinstance(row, dict):
        return None
    for key in (
        "context_window",
        "context_length",
        "max_context_length",
        "max_model_len",
        "max_input_tokens",
    ):
        value = row.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, float) and value > 0:
            return int(value)
    return None


def _is_excluded_discovered_model_id(model_id: str) -> bool:
    normalized = model_id.strip().lower()
    return normalized.rsplit("/", 1)[-1] in _EXCLUDED_DISCOVERED_MODEL_IDS


def _normalized_metadata_terms(value: Any) -> set[str]:
    """Normalize structured provider metadata without inspecting the model id."""
    terms: set[str] = set()
    if isinstance(value, str):
        normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
        if normalized:
            terms.add(normalized)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            terms.update(_normalized_metadata_terms(item))
    elif isinstance(value, dict):
        for key, enabled in value.items():
            if enabled is True:
                terms.update(_normalized_metadata_terms(key))
            elif enabled is not False and enabled is not None:
                terms.update(_normalized_metadata_terms(enabled))
    return terms


def _row_metadata_terms(row: dict[str, Any], *fields: str) -> set[str]:
    terms: set[str] = set()
    for field in fields:
        terms.update(_normalized_metadata_terms(row.get(field)))
    for container_name in (
        "architecture",
        "metadata",
        "model_info",
        "modelInfo",
        "modalities",
    ):
        container = row.get(container_name)
        if not isinstance(container, dict):
            continue
        for field in fields:
            terms.update(_normalized_metadata_terms(container.get(field)))
    return terms


def _task_profile(terms: set[str]) -> tuple[list[ModelCapability], str] | None:
    profiles: tuple[tuple[set[str], list[ModelCapability], str], ...] = (
        (
            {
                "speech_to_text",
                "audio_transcription",
                "transcription",
                "transcribe",
                "asr",
                "speech_recognition",
            },
            ["speech_to_text"],
            "speech_to_text",
        ),
        (
            {"text_to_speech", "speech_synthesis", "tts", "voice_synthesis"},
            ["text_to_speech"],
            "text_to_speech",
        ),
        (
            {"image_generation", "text_to_image", "image_synthesis", "image_gen"},
            [],
            "image_generation",
        ),
        ({"embedding", "embeddings", "text_embedding", "vectorization"}, [], "embedding"),
        ({"rerank", "reranking", "ranking"}, [], "rerank"),
        ({"moderation", "safety", "content_moderation"}, [], "moderation"),
        (
            {
                "chat",
                "chat_completion",
                "chat_completions",
                "completion",
                "completions",
                "text_generation",
                "language_model",
                "llm",
                "vision",
                "multimodal",
                "responses",
            },
            ["text"],
            "text",
        ),
    )
    for markers, capabilities, model_type in profiles:
        if terms & markers:
            return list(capabilities), model_type
    return None


def _provider_declared_model_profile(
    row: dict[str, Any],
) -> tuple[list[ModelCapability], str] | None:
    """Resolve capabilities from provider-declared task and modality fields."""
    task_terms = _row_metadata_terms(
        row,
        "type",
        "model_type",
        "modelType",
        "task",
        "tasks",
        "purpose",
        "category",
    )
    task_profile = _task_profile(task_terms)
    if task_profile is not None and task_profile[1] != "text":
        return task_profile

    input_modalities = _row_metadata_terms(
        row,
        "input_modalities",
        "inputModalities",
        "input_types",
        "inputTypes",
        "input",
        "inputs",
    )
    output_modalities = _row_metadata_terms(
        row,
        "output_modalities",
        "outputModalities",
        "output_types",
        "outputTypes",
        "output",
        "outputs",
    )
    if output_modalities & {"embedding", "embeddings", "vector", "vectors"}:
        return [], "embedding"
    if "image" in output_modalities and "text" not in output_modalities:
        return [], "image_generation"
    if "audio" in output_modalities and "text" not in output_modalities:
        if not input_modalities or "text" in input_modalities:
            return ["text_to_speech"], "text_to_speech"
        return [], "audio"
    if "audio" in input_modalities and output_modalities == {"text"}:
        return ["speech_to_text"], "speech_to_text"
    if "text" in output_modalities:
        if "audio" in output_modalities:
            return [], "realtime_audio"
        return ["text"], "text"

    undirected_modalities = _row_metadata_terms(row, "modality", "modalities")
    if undirected_modalities:
        if "text" in undirected_modalities:
            return ["text"], "text"
        if undirected_modalities & {"image", "images"}:
            return [], "image"
        if undirected_modalities & {"audio", "speech"}:
            return [], "audio"

    if task_profile is not None:
        return task_profile

    capability_terms = _row_metadata_terms(
        row,
        "capability",
        "capabilities",
        "feature",
        "features",
        "supported_features",
        "supportedFeatures",
    )
    capability_profile = _task_profile(capability_terms)
    if capability_profile is not None:
        return capability_profile
    if "text" in capability_terms:
        return ["text"], "text"
    if capability_terms & {"audio", "speech", "realtime"}:
        return [], "audio"
    if capability_terms & {"image", "images"}:
        return [], "image"
    return None


def _model_row_payload(row: Any) -> dict[str, Any] | None:
    model_id = _model_id_from_row(row)
    if not model_id or _is_excluded_discovered_model_id(model_id):
        return None
    label: str | None = None
    owned_by: str | None = None
    if isinstance(row, dict):
        raw_label = row.get("display_name") or row.get("label") or row.get("name")
        if isinstance(raw_label, str) and raw_label.strip() and raw_label.strip() != model_id:
            label = raw_label.strip()
        raw_owner = row.get("owned_by") or row.get("owner") or row.get("organization")
        if isinstance(raw_owner, str) and raw_owner.strip():
            owned_by = raw_owner.strip()
    declared_profile = _provider_declared_model_profile(row) if isinstance(row, dict) else None
    return {
        "id": model_id,
        "label": label,
        "owned_by": owned_by,
        "context_window": _model_context_window(row),
        "capabilities": declared_profile[0] if declared_profile is not None else None,
        "model_type": declared_profile[1] if declared_profile is not None else None,
        "capability_source": "provider" if declared_profile is not None else None,
    }


def _extract_model_rows(body: Any) -> list[dict[str, Any]]:
    raw_rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(raw_rows, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_row in raw_rows:
        row = _model_row_payload(raw_row)
        if row is None or row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)
    return rows


def provider_models_payload(query: QueryParams) -> dict[str, Any]:
    """Fetch an OpenAI-compatible provider's model list for Settings.

    The result is advisory only: users can always type a custom model id. This
    helper deliberately avoids mutating config so probing model lists never
    changes runtime behavior.
    """
    provider_name = (_query_first(query, "provider") or "").strip()
    probe_api_base = (_query_first_alias(query, "api_base", "apiBase") or "").strip()
    probe_api_key_raw = _query_first_alias(query, "api_key", "apiKey")
    probe_api_key = probe_api_key_raw.strip() if probe_api_key_raw is not None else None
    probe_api_type = (_query_first(query, "api_type") or "").strip()
    if not provider_name and probe_api_base:
        provider_name = "custom"
    if not provider_name:
        raise WebUISettingsError("provider is required")
    if probe_api_type and probe_api_type not in {"auto", "chat_completions", "responses"}:
        raise WebUISettingsError("api_type must be auto, chat_completions, or responses")

    config = load_config()
    resolved_provider = _resolve_settings_provider(config, provider_name)
    if resolved_provider is None:
        raise WebUISettingsError("unknown provider")
    spec, provider_key, provider_config = resolved_provider
    if probe_api_base or probe_api_key is not None or probe_api_type:
        provider_config = ProviderConfig(
            api_key=probe_api_key if probe_api_key is not None else provider_config.api_key,
            api_base=probe_api_base or provider_config.api_base,
            api_type=probe_api_type or provider_config.api_type,
            extra_headers=provider_config.extra_headers,
            extra_body=provider_config.extra_body,
            extra_query=provider_config.extra_query,
        )

    base_payload: dict[str, Any] = {
        "provider": provider_key,
        "label": spec.label,
        "catalog_kind": _model_catalog_kind(spec),
        "models": [],
        "model_count": 0,
        "message": None,
        "fetched_at": time.time(),
    }
    if (
        spec.is_transcription_only
        or (
            spec.backend in _MODEL_LIST_UNSUPPORTED_BACKENDS
            and spec.name != "minimax_anthropic"
        )
        or spec.is_oauth
    ):
        return {
            **base_payload,
            "status": "unsupported",
            "catalog_kind": "unsupported",
            "message": "Model list is not available for this provider. Type a model ID manually.",
        }

    api_base = _resolve_env_placeholders(provider_config.api_base) or spec.default_api_base
    if spec.name == "openai" and not api_base:
        api_base = "https://api.openai.com/v1"
    if not api_base:
        return {
            **base_payload,
            "status": "missing_api_base",
            "message": "Configure an API base URL to load models.",
        }

    api_key = _resolve_env_placeholders(provider_config.api_key)
    if _provider_requires_api_key(spec) and not api_key:
        return {
            **base_payload,
            "status": "not_configured",
            "message": "Configure this provider before loading models.",
        }

    headers = {"Accept": "application/json"}
    if api_key:
        if spec.name == "minimax_anthropic":
            headers["X-Api-Key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    models_url = f"{api_base.rstrip('/')}/models"
    if spec.name == "minimax_anthropic" and not api_base.rstrip("/").endswith("/v1"):
        models_url = f"{api_base.rstrip('/')}/v1/models"

    try:
        response = httpx.get(
            models_url,
            headers=headers,
            timeout=10.0,
            follow_redirects=False,
        )
        response.raise_for_status()
        rows = _extract_model_rows(response.json())
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            return {
                **base_payload,
                "status": "not_configured",
                "message": "The provider rejected the configured credential.",
            }
        return {
            **base_payload,
            "status": "error",
            "message": f"Model list request failed with HTTP {status}.",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            **base_payload,
            "status": "error",
            "message": f"Could not load models: {exc}",
        }

    detected_provider = _canonical_provider_name_for_api_base(api_base) or provider_key
    for row in rows:
        if row["capabilities"] is None:
            capabilities, model_type = _infer_model_profile(
                detected_provider, row["id"]
            )
            row["capabilities"] = capabilities
            row["model_type"] = model_type
            row["capability_source"] = "heuristic"

    return {
        **base_payload,
        "provider": detected_provider,
        "status": "available",
        "models": rows,
        "model_count": len(rows),
    }


def _parse_bool(value: str, field: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"1", "0", "true", "false", "yes", "no"}:
        raise WebUISettingsError(f"{field} must be boolean")
    return normalized in {"1", "true", "yes"}


def _parse_context_window_tokens(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise WebUISettingsError("context_window_tokens must be an integer") from None
    if parsed not in _CONTEXT_WINDOW_TOKEN_OPTIONS:
        raise WebUISettingsError("context_window_tokens must be 65536, 200000, or 262144")
    return parsed


def _model_configuration_slug(label: str) -> str:
    normalized = _MODEL_CONFIGURATION_SLUG_RE.sub("-", label.strip().lower())
    normalized = normalized.strip("-_")
    if not normalized:
        raise WebUISettingsError("configuration name is required")
    if normalized == "default":
        raise WebUISettingsError("configuration name is reserved")
    if len(normalized) > 48:
        normalized = normalized[:48].rstrip("-_")
    return normalized


def _parse_model_capabilities(
    value: str | None,
    *,
    default: list[ModelCapability] | None = None,
) -> list[ModelCapability]:
    if value is None:
        return list(default or ["text"])
    raw = [part.strip() for part in value.split(",") if part.strip()]
    invalid = [part for part in raw if part not in MODEL_CAPABILITIES]
    if invalid:
        raise WebUISettingsError(f"unknown model capability: {invalid[0]}")
    return list(dict.fromkeys(raw)) or list(default or ["text"])


def _parse_model_capability_source(
    value: str | None,
    *,
    default: ModelCapabilitySource,
) -> ModelCapabilitySource:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized not in MODEL_CAPABILITY_SOURCES:
        raise WebUISettingsError("unknown model capability source")
    return cast(ModelCapabilitySource, normalized)


def _infer_model_profile(provider: str, model: str) -> tuple[list[ModelCapability], str]:
    normalized = model.strip().lower()
    synthesis_markers = ("text-to-speech", "text_to_speech", "-tts", "_tts")
    if normalized == "tts" or any(marker in normalized for marker in synthesis_markers):
        return ["text_to_speech"], "text_to_speech"
    speech_markers = ("whisper", "transcribe", "transcription", "-asr", "_asr", "sensevoice")
    if any(marker in normalized for marker in speech_markers):
        return ["speech_to_text"], "speech_to_text"
    if "audio" in normalized:
        return [], "audio"
    image_markers = ("dall-e", "gpt-image", "image-generation", "image_generation", "text-to-image")
    if any(marker in normalized for marker in image_markers):
        return [], "image_generation"
    embedding_markers = ("embedding", "embed-")
    if any(marker in normalized for marker in embedding_markers):
        return [], "embedding"
    rerank_markers = ("rerank", "re-rank")
    if any(marker in normalized for marker in rerank_markers):
        return [], "rerank"
    return ["text"], "text"


def _infer_model_capabilities(provider: str, model: str) -> list[ModelCapability]:
    """Compatibility fallback when a provider does not declare model metadata."""
    return _infer_model_profile(provider, model)[0]


def _matching_capability_preset(
    config: Any,
    *,
    provider: str,
    model: str,
    capability: ModelCapability,
) -> str | None:
    for name, preset in config.model_presets.items():
        if (
            preset.provider == provider
            and preset.model == model
            and capability in preset.capabilities
        ):
            return name
    return None


def _ensure_capability_preset(
    config: Any,
    *,
    provider: str,
    model: str,
    capability: ModelCapability,
    label: str,
) -> tuple[str, bool]:
    existing = _matching_capability_preset(
        config,
        provider=provider,
        model=model,
        capability=capability,
    )
    if existing:
        return existing, False
    base_name = _model_configuration_slug(f"{provider}-{model}-{capability}")
    name = base_name
    suffix = 2
    while name in config.model_presets:
        name = f"{base_name[:43]}-{suffix}"
        suffix += 1
    base = config.resolve_default_preset()
    config.model_presets[name] = ModelPresetConfig(
        label=label,
        model=model,
        provider=provider,
        max_tokens=base.max_tokens,
        context_window_tokens=base.context_window_tokens,
        temperature=base.temperature,
        reasoning_effort=base.reasoning_effort,
        capabilities=[capability],
        capability_source="manual",
    )
    return name, True


def ensure_model_capability_defaults(config: Any) -> bool:
    """Normalize user-facing model purposes and remove retired purpose tags."""
    changed = False
    for capability in _REMOVED_MODEL_CAPABILITIES:
        if getattr(config.model_defaults, capability) is not None:
            setattr(config.model_defaults, capability, None)
            changed = True
    for preset in config.model_presets.values():
        original_capabilities = list(preset.capabilities)
        capabilities = [
            capability
            for capability in preset.capabilities
            if capability not in _REMOVED_MODEL_CAPABILITIES
        ]
        if preset.capability_source is None:
            # Preserve pre-existing explicit non-text classifications. Legacy
            # default-text rows still receive the compatibility inference once.
            if capabilities == ["text"] or any(
                capability in _REMOVED_MODEL_CAPABILITIES
                for capability in original_capabilities
            ):
                capabilities = _infer_model_capabilities(preset.provider, preset.model)
                preset.capability_source = "heuristic"
            else:
                preset.capability_source = "manual"
            changed = True
        elif preset.capability_source == "heuristic":
            capabilities = _infer_model_capabilities(preset.provider, preset.model)
        if preset.capabilities != capabilities:
            preset.capabilities = capabilities
            changed = True

    # A default may have pointed at a legacy preset whose inferred purpose has
    # changed.  Do not keep a default that no longer supports its capability.
    for capability in MODEL_CAPABILITIES:
        preset_name = getattr(config.model_defaults, capability)
        if not preset_name or preset_name == "default":
            continue
        preset = config.model_presets.get(preset_name)
        if preset is None or capability not in preset.capabilities:
            setattr(config.model_defaults, capability, None)
            changed = True

    text_default = config.agents.defaults.model_preset or "default"
    if config.model_defaults.text != text_default:
        config.model_defaults.text = text_default
        changed = True

    transcription = resolve_transcription_config(config)
    # A disabled transcription feature must not materialize a fallback ASR
    # model in Model Configuration. Desktop deployments that only provision a
    # text provider intentionally keep transcription disabled until the user
    # explicitly configures it.
    if (
        transcription.enabled
        and transcription.configured
        and transcription.provider
        and transcription.model
    ):
        name, created = _ensure_capability_preset(
            config,
            provider=transcription.provider,
            model=transcription.model,
            capability="speech_to_text",
            label=f"{transcription.provider} / {transcription.model}",
        )
        changed = created or changed
        if config.model_defaults.speech_to_text != name:
            config.model_defaults.speech_to_text = name
            changed = True

    return changed


def _validate_configured_provider(config: Any, provider: str) -> None:
    if provider == "auto":
        return
    resolved_provider = _resolve_settings_provider(config, provider)
    if resolved_provider is None:
        raise WebUISettingsError("unknown provider")
    spec, _, provider_config = resolved_provider
    if spec.is_transcription_only:
        raise WebUISettingsError("provider does not support chat models")
    if not _provider_configured_for_settings(spec, provider_config):
        raise WebUISettingsError("provider is not configured")


def _validate_provider_for_capabilities(
    config: Any,
    provider: str,
    capabilities: list[ModelCapability],
) -> None:
    """Validate a provider against every capability assigned to a model preset."""
    if any(capability in {"text", "vision"} for capability in capabilities):
        _validate_configured_provider(config, provider)
    if "text_to_speech" in capabilities:
        # TTS models are catalogued separately even though desktop speech
        # synthesis is not yet an executable model default.
        _validate_configured_provider(config, provider)
    if "speech_to_text" in capabilities:
        if resolve_transcription_provider(provider) is None:
            raise WebUISettingsError("provider does not support speech recognition")
        resolved_provider = _resolve_settings_provider(config, provider)
        if resolved_provider is None:
            raise WebUISettingsError("unknown provider")
        spec, _, provider_config = resolved_provider
        if not _provider_configured_for_settings(spec, provider_config):
            raise WebUISettingsError("provider is not configured")
    if "image_generation" in capabilities and get_image_gen_provider(provider) is None:
        raise WebUISettingsError("provider does not support image generation")


def _image_generation_provider_rows(config: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in image_gen_provider_names():
        spec = find_by_name(name)
        provider_config = getattr(config.providers, name, None)
        configured = (
            _provider_configured_for_settings(spec, provider_config)
            if spec is not None and provider_config is not None
            else bool(getattr(provider_config, "api_key", None))
        )
        rows.append(
            {
                "name": name,
                "label": spec.label if spec is not None else name,
                "configured": configured,
                "auth_type": "oauth" if spec is not None and spec.is_oauth else "api_key",
                "api_key_hint": _mask_secret_hint(
                    getattr(provider_config, "api_key", None)
                ),
                "api_base": getattr(provider_config, "api_base", None),
                "default_api_base": (
                    spec.default_api_base if spec and spec.default_api_base else None
                ),
            }
        )
    return rows


_DEFAULT_REASONING_EFFORT_VALUES: tuple[str, ...] = ("", "low", "medium", "high")


def _reasoning_effort_values_for(provider_name: str, model: str) -> list[str]:
    """Return user-facing reasoning_effort options for this provider+model.

    Mistral chat models accept only "high"/"none"; Magistral rejects the
    kwarg entirely (reasoning is implicit). For everyone else, return the
    full OpenAI vocab.
    """
    spec = find_by_name(provider_name) if provider_name else None
    if spec is None:
        return list(_DEFAULT_REASONING_EFFORT_VALUES)

    model_lower = (model or "").lower()
    implicit = getattr(spec, "implicit_reasoning_models", ())
    if implicit and any(pat in model_lower for pat in implicit):
        # Reasoning is always on; only "Default" makes sense.
        return [""]

    remap = getattr(spec, "reasoning_effort_remap", ())
    if remap:
        # Reverse the remap: surface the distinct wire-vocab outputs as the
        # user's options. Mistral collapses to "high"/"none" → UI shows
        # "Default" + "High".
        wire_values: list[str] = []
        for _user_val, wire_val in remap:
            if wire_val and wire_val != "none" and wire_val not in wire_values:
                wire_values.append(wire_val)
        return ["", *wire_values]

    return list(_DEFAULT_REASONING_EFFORT_VALUES)


def _transcription_provider_rows(config: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in transcription_provider_names():
        spec = find_by_name(name)
        provider_config = getattr(config.providers, name, None)
        rows.append({
            "name": name,
            "label": spec.label if spec is not None else name,
            "configured": bool(getattr(provider_config, "api_key", None)),
            "api_key_hint": _mask_secret_hint(getattr(provider_config, "api_key", None)),
            "api_base": getattr(provider_config, "api_base", None),
            "default_api_base": spec.default_api_base if spec and spec.default_api_base else None,
        })
    return rows


def settings_payload(
    *,
    requires_restart: bool = False,
    surface: str | None = "browser",
    runtime_capability_overrides: dict[str, Any] | None = None,
    restart_required_sections: list[str] | None = None,
    apply_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = load_config()
    defaults = config.agents.defaults
    active_preset_name = defaults.model_preset or "default"
    try:
        effective_preset = config.resolve_preset()
    except Exception:
        effective_preset = config.resolve_default_preset()
        active_preset_name = "default"

    provider_name = (
        config.get_provider_name(effective_preset.model, preset=effective_preset)
        or effective_preset.provider
    )
    provider = config.get_provider(effective_preset.model, preset=effective_preset)
    selected_provider = provider_name
    if effective_preset.provider != "auto":
        spec = find_by_name(effective_preset.provider)
        selected_provider = spec.name if spec else provider_name

    providers = []
    for spec in PROVIDERS:
        provider_config = getattr(config.providers, spec.name, None)
        if provider_config is None:
            continue
        providers.append(_provider_settings_row(spec.name, spec, provider_config))
    for provider_key, provider_config in _dynamic_provider_items(config):
        providers.append(
            _provider_settings_row(
                provider_key,
                create_dynamic_spec(provider_key),
                provider_config,
                custom=True,
            )
        )

    search_config = config.tools.web.search
    image_config = config.tools.image_generation
    transcription = resolve_transcription_config(config)
    streaming_transcription = resolve_streaming_transcription_profile(transcription)
    search_provider = (
        search_config.provider
        if search_config.provider in _WEB_SEARCH_PROVIDER_BY_NAME
        else "duckduckgo"
    )
    image_providers = _image_generation_provider_rows(config)
    selected_image_provider = next(
        (
            provider
            for provider in image_providers
            if provider["name"] == image_config.provider
        ),
        None,
    )
    model_presets = [
        {
            "name": "default",
            "label": "Default",
            "active": active_preset_name == "default",
            "is_default": True,
            "model": defaults.model,
            "provider": defaults.provider,
            "max_tokens": defaults.max_tokens,
            "context_window_tokens": defaults.context_window_tokens,
            "temperature": defaults.temperature,
            "reasoning_effort": defaults.reasoning_effort,
            "reasoning_effort_values": _reasoning_effort_values_for(
                defaults.provider, defaults.model
            ),
            "capabilities": ["text"],
        }
    ]
    for name, preset in config.model_presets.items():
        model_presets.append(
            {
                "name": name,
                "label": preset.label or name,
                "active": active_preset_name == name,
                "is_default": False,
                "model": preset.model,
                "provider": preset.provider,
                "max_tokens": preset.max_tokens,
                "context_window_tokens": preset.context_window_tokens,
                "temperature": preset.temperature,
                "reasoning_effort": preset.reasoning_effort,
                "reasoning_effort_values": _reasoning_effort_values_for(
                    preset.provider, preset.model
                ),
                "capabilities": [
                    capability
                    for capability in preset.capabilities
                    if capability in MODEL_CAPABILITIES
                ],
                "capability_source": preset.capability_source or "heuristic",
            }
        )

    exec_config = config.tools.exec
    sandbox_status = workspace_sandbox_status(
        restrict_to_workspace=config.tools.restrict_to_workspace,
        workspace=config.workspace_path,
    )
    payload = {
        "agent": {
            "model": effective_preset.model,
            "provider": selected_provider,
            "resolved_provider": provider_name,
            "has_api_key": bool(provider and provider.api_key),
            "model_preset": active_preset_name,
            "max_tokens": effective_preset.max_tokens,
            "context_window_tokens": effective_preset.context_window_tokens,
            "temperature": effective_preset.temperature,
            "reasoning_effort": effective_preset.reasoning_effort,
            "timezone": defaults.timezone,
            "bot_name": defaults.bot_name,
            "bot_icon": defaults.bot_icon,
            "tool_hint_max_length": defaults.tool_hint_max_length,
        },
        "model_presets": model_presets,
        "model_defaults": {
            "text": config.agents.defaults.model_preset or "default",
            "speech_to_text": config.model_defaults.speech_to_text,
            "text_to_speech": config.model_defaults.text_to_speech,
        },
        "providers": providers,
        "web_search": {
            "provider": search_provider,
            "api_key_hint": _mask_secret_hint(search_config.api_key),
            "base_url": search_config.base_url or None,
            "max_results": search_config.max_results,
            "timeout": search_config.timeout,
            "providers": list(_WEB_SEARCH_PROVIDER_OPTIONS),
        },
        "web": {
            "enable": config.tools.web.enable,
            "proxy": config.tools.web.proxy,
            "user_agent": config.tools.web.user_agent,
            "search": {
                "max_results": search_config.max_results,
                "timeout": search_config.timeout,
            },
            "fetch": {
                "use_jina_reader": config.tools.web.fetch.use_jina_reader,
            },
        },
        "image_generation": {
            "enabled": image_config.enabled,
            "provider": image_config.provider,
            "provider_configured": bool(
                selected_image_provider and selected_image_provider["configured"]
            ),
            "model": image_config.model,
            "default_aspect_ratio": image_config.default_aspect_ratio,
            "default_image_size": image_config.default_image_size,
            "max_images_per_turn": image_config.max_images_per_turn,
            "save_dir": image_config.save_dir,
            "providers": image_providers,
        },
        "transcription": {
            "enabled": transcription.enabled,
            "provider": transcription.provider,
            "provider_configured": transcription.configured,
            "model": transcription.model,
            "language": transcription.language,
            "max_duration_sec": transcription.max_duration_sec,
            "max_upload_mb": transcription.max_upload_mb,
            "providers": _transcription_provider_rows(config),
            "streaming": {
                "supported": streaming_transcription is not None,
                "profile": (
                    streaming_transcription.name
                    if streaming_transcription is not None
                    else None
                ),
                "upstream_model": (
                    streaming_transcription.model
                    if streaming_transcription is not None
                    else None
                ),
                "batch_fallback": bool(
                    streaming_transcription
                    and streaming_transcription.batch_fallback
                ),
            },
        },
        "runtime": {
            "config_path": str(get_config_path().expanduser()),
            "workspace_path": str(config.workspace_path),
            "gateway_host": config.gateway.host,
            "gateway_port": config.gateway.port,
            "heartbeat": {
                "enabled": config.gateway.heartbeat.enabled,
                "interval_s": config.gateway.heartbeat.interval_s,
                "keep_recent_messages": config.gateway.heartbeat.keep_recent_messages,
            },
            "dream": {
                "schedule": defaults.dream.describe_schedule(),
            },
            "unified_session": defaults.unified_session,
        },
        "usage": token_usage_payload(timezone_name=defaults.timezone),
        "advanced": {
            "restrict_to_workspace": config.tools.restrict_to_workspace,
            "workspace_sandbox": sandbox_status.as_dict(),
            "webui_allow_local_service_access": config.tools.webui_allow_local_service_access,
            "allow_local_preview_access": config.tools.webui_allow_local_service_access,
            "webui_default_access_mode": read_webui_default_access_mode(),
            "private_service_protection_enabled": True,
            "ssrf_whitelist_count": len(config.tools.ssrf_whitelist),
            "mcp_server_count": len(config.tools.mcp_servers),
            "exec_enabled": exec_config.enable,
            "exec_sandbox": exec_config.sandbox or None,
            "exec_path_prepend_set": bool(exec_config.path_prepend),
            "exec_path_append_set": bool(exec_config.path_append),
        },
        "requires_restart": requires_restart,
        "version": _version_payload(),
    }
    return decorate_settings_payload(
        payload,
        surface=surface,
        runtime_capability_overrides=runtime_capability_overrides,
        restart_required_sections=restart_required_sections,
        apply_state=apply_state,
    )


def settings_usage_payload() -> dict[str, Any]:
    """Return the lightweight token usage slice for Overview refreshes."""
    config = load_config()
    return token_usage_payload(timezone_name=config.agents.defaults.timezone)


def update_agent_settings(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    defaults = config.agents.defaults
    changed = False
    restart_required = False

    if "model_preset" in query or "modelPreset" in query:
        preset = (_query_first_alias(query, "model_preset", "modelPreset") or "").strip()
        preset_value = None if not preset or preset == "default" else preset
        if preset_value is not None and preset_value not in config.model_presets:
            raise WebUISettingsError("unknown model preset")
        if defaults.model_preset != preset_value:
            defaults.model_preset = preset_value
            config.model_defaults.text = preset_value or "default"
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("model is required")
        if defaults.model != model:
            defaults.model = model
            changed = True

    provider = _query_first(query, "provider")
    if provider is not None:
        provider = provider.strip()
        if not provider:
            raise WebUISettingsError("provider is required")
        _validate_configured_provider(config, provider)
        if defaults.provider != provider:
            defaults.provider = provider
            changed = True

    context_window_tokens = _parse_context_window_tokens(
        _query_first_alias(query, "context_window_tokens", "contextWindowTokens")
    )
    if (
        context_window_tokens is not None
        and defaults.context_window_tokens != context_window_tokens
    ):
        defaults.context_window_tokens = context_window_tokens
        changed = True

    timezone = _query_first(query, "timezone")
    if timezone is not None:
        timezone = timezone.strip()
        if not timezone:
            raise WebUISettingsError("timezone is required")
        try:
            ZoneInfo(timezone)
        except Exception:
            raise WebUISettingsError("invalid timezone") from None
        if defaults.timezone != timezone:
            defaults.timezone = timezone
            changed = True
            restart_required = True

    bot_name = _query_first_alias(query, "bot_name", "botName")
    if bot_name is not None:
        bot_name = bot_name.strip()
        if not bot_name:
            raise WebUISettingsError("bot_name is required")
        if defaults.bot_name != bot_name:
            defaults.bot_name = bot_name
            changed = True
            restart_required = True

    bot_icon = _query_first_alias(query, "bot_icon", "botIcon")
    if bot_icon is not None:
        bot_icon = bot_icon.strip()
        if defaults.bot_icon != bot_icon:
            defaults.bot_icon = bot_icon
            changed = True
            restart_required = True

    tool_hint_max_length = _query_first_alias(
        query,
        "tool_hint_max_length",
        "toolHintMaxLength",
    )
    if tool_hint_max_length is not None:
        try:
            parsed = int(tool_hint_max_length)
        except ValueError:
            raise WebUISettingsError("tool_hint_max_length must be an integer") from None
        if parsed < 20 or parsed > 500:
            raise WebUISettingsError("tool_hint_max_length must be between 20 and 500")
        if defaults.tool_hint_max_length != parsed:
            defaults.tool_hint_max_length = parsed
            changed = True
            restart_required = True

    if changed:
        save_config(config)
    return settings_payload(requires_restart=restart_required)


def create_model_configuration(query: QueryParams) -> dict[str, Any]:
    label = (_query_first_alias(query, "label", "displayName") or "").strip()
    raw_name = (_query_first(query, "name") or label).strip()
    model = (_query_first(query, "model") or "").strip()
    provider = (_query_first(query, "provider") or "").strip()
    raw_capabilities = _query_first(query, "capabilities")
    raw_capability_source = _query_first_alias(
        query, "capability_source", "capabilitySource"
    )

    if not label:
        label = raw_name
    if not model:
        raise WebUISettingsError("model is required")
    if not provider:
        raise WebUISettingsError("provider is required")
    if raw_capability_source is not None and raw_capabilities is None:
        raise WebUISettingsError("model capability source requires capabilities")

    capabilities = (
        _parse_model_capabilities(raw_capabilities)
        if raw_capabilities is not None
        else _infer_model_capabilities(provider, model)
    )
    capability_source = _parse_model_capability_source(
        raw_capability_source,
        default="manual" if raw_capabilities is not None else "heuristic",
    )

    name = _model_configuration_slug(raw_name or label)
    config = load_config()
    for preset in config.model_presets.values():
        if preset.provider == provider and preset.model == model:
            raise WebUISettingsError("configuration already exists", status=409)
    if name in config.model_presets:
        raise WebUISettingsError("configuration already exists", status=409)
    _validate_provider_for_capabilities(config, provider, capabilities)

    base = config.resolve_default_preset()
    config.model_presets[name] = ModelPresetConfig(
        label=label,
        model=model,
        provider=provider,
        max_tokens=base.max_tokens,
        context_window_tokens=base.context_window_tokens,
        temperature=base.temperature,
        reasoning_effort=base.reasoning_effort,
        capabilities=capabilities,
        capability_source=capability_source,
    )
    if "text" in capabilities and not config.model_defaults.text:
        config.agents.defaults.model_preset = name
        config.model_defaults.text = name
    if "speech_to_text" in capabilities and not config.model_defaults.speech_to_text:
        config.model_defaults.speech_to_text = name
    save_config(config)
    return settings_payload()


def update_model_configuration(query: QueryParams) -> dict[str, Any]:
    name = (_query_first(query, "name") or "").strip()
    if not name or name == "default":
        raise WebUISettingsError("model configuration is required")

    config = load_config()
    preset = config.model_presets.get(name)
    if preset is None:
        raise WebUISettingsError("unknown model configuration")

    changed = False
    label = _query_first_alias(query, "label", "displayName")
    if label is not None:
        label = label.strip()
        if not label:
            raise WebUISettingsError("label is required")
        if preset.label != label:
            preset.label = label
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("model is required")
        if preset.model != model:
            preset.model = model
            changed = True

    provider_raw = _query_first(query, "provider")
    proposed_provider = preset.provider
    if provider_raw is not None:
        proposed_provider = provider_raw.strip()
        if not proposed_provider:
            raise WebUISettingsError("provider is required")

    capabilities_raw = _query_first(query, "capabilities")
    capability_source_raw = _query_first_alias(
        query, "capability_source", "capabilitySource"
    )
    if capability_source_raw is not None and capabilities_raw is None:
        raise WebUISettingsError("model capability source requires capabilities")
    proposed_capabilities = list(preset.capabilities)
    proposed_capability_source = preset.capability_source
    if capabilities_raw is not None:
        proposed_capabilities = _parse_model_capabilities(
            capabilities_raw,
            default=list(preset.capabilities),
        )
        for capability in MODEL_CAPABILITIES:
            if (
                getattr(config.model_defaults, capability) == name
                and capability not in proposed_capabilities
            ):
                raise WebUISettingsError(
                    f"cannot remove {capability} while this model is its default"
                )
        proposed_capability_source = _parse_model_capability_source(
            capability_source_raw,
            default="manual",
        )

    if provider_raw is not None or capabilities_raw is not None:
        _validate_provider_for_capabilities(
            config,
            proposed_provider,
            proposed_capabilities,
        )
    if preset.provider != proposed_provider:
        preset.provider = proposed_provider
        changed = True
    if preset.capabilities != proposed_capabilities:
        preset.capabilities = proposed_capabilities
        changed = True
    if preset.capability_source != proposed_capability_source:
        preset.capability_source = proposed_capability_source
        changed = True

    context_window_tokens = _parse_context_window_tokens(
        _query_first_alias(query, "context_window_tokens", "contextWindowTokens")
    )
    if (
        context_window_tokens is not None
        and preset.context_window_tokens != context_window_tokens
    ):
        preset.context_window_tokens = context_window_tokens
        changed = True

    should_select_text_preset = any(
        value is not None
        for value in (label, model, provider_raw, context_window_tokens)
    )
    if (
        should_select_text_preset
        and "text" in preset.capabilities
        and config.agents.defaults.model_preset != name
    ):
        config.agents.defaults.model_preset = name
        config.model_defaults.text = name
        changed = True

    if changed:
        save_config(config)
    return settings_payload()


def delete_model_configuration(query: QueryParams) -> dict[str, Any]:
    name = (_query_first(query, "name") or "").strip()
    if not name or name == "default":
        raise WebUISettingsError("model configuration name is required")

    config = load_config()
    if name in config.model_presets:
        del config.model_presets[name]
        if config.agents.defaults.model_preset == name:
            config.agents.defaults.model_preset = None
            config.model_defaults.text = "default"
        for capability in _STORED_MODEL_CAPABILITIES:
            if getattr(config.model_defaults, capability) == name:
                setattr(config.model_defaults, capability, None)
        save_config(config)
    return settings_payload()


def update_model_default(query: QueryParams) -> dict[str, Any]:
    capability = (_query_first(query, "capability") or "").strip()
    name = (_query_first(query, "name") or "").strip()
    if capability not in MODEL_CAPABILITIES:
        raise WebUISettingsError("unknown model capability")
    if capability == "text_to_speech":
        raise WebUISettingsError("speech synthesis defaults are not available yet", status=409)
    if not name:
        raise WebUISettingsError("model configuration is required")

    config = load_config()
    if name == "default":
        if capability != "text":
            raise WebUISettingsError("default model is only available for text")
        preset = config.resolve_default_preset()
    else:
        preset = config.model_presets.get(name)
        if preset is None:
            raise WebUISettingsError("unknown model configuration")
        if capability not in preset.capabilities:
            raise WebUISettingsError("model configuration does not support this capability")

    setattr(config.model_defaults, capability, name)
    if capability == "text":
        config.agents.defaults.model_preset = None if name == "default" else name
    elif capability == "speech_to_text":
        if resolve_transcription_provider(preset.provider) is None:
            raise WebUISettingsError("provider does not support speech recognition")
        config.transcription.provider = preset.provider
        config.transcription.model = preset.model
    elif capability == "image_generation":
        if get_image_gen_provider(preset.provider) is None:
            raise WebUISettingsError("provider does not support image generation")
        config.tools.image_generation.provider = preset.provider
        config.tools.image_generation.model = preset.model
    save_config(config)
    return settings_payload()


def _custom_provider_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-_").lower()
    if not slug:
        slug = "custom-provider"
    return slug


def create_provider_settings(query: QueryParams) -> dict[str, Any]:
    raw_name = (_query_first(query, "name") or _query_first(query, "displayName") or _query_first(query, "label") or "").strip()
    api_base = (_query_first_alias(query, "api_base", "apiBase") or "").strip()
    api_key = (_query_first_alias(query, "api_key", "apiKey") or "").strip() or None
    api_type = (_query_first(query, "api_type") or "auto").strip()

    if not api_base:
        raise WebUISettingsError("api_base is required")
    if api_type not in {"auto", "chat_completions", "responses"}:
        raise WebUISettingsError("api_type must be auto, chat_completions, or responses")

    config = load_config()
    canonical_name = _canonical_provider_name_for_api_base(api_base)
    if canonical_name:
        spec = find_by_name(canonical_name)
        provider_config = getattr(config.providers, canonical_name, None)
        if spec is None or not isinstance(provider_config, ProviderConfig):
            raise WebUISettingsError("unknown provider")
        created = not _provider_configured_for_settings(spec, provider_config)
        provider_config.label = raw_name or spec.label
        provider_config.api_key = api_key
        provider_config.api_base = api_base
        # Built-in providers select their protocol from the registry.
        provider_config.api_type = "auto"
        save_config(config)
        payload = settings_payload()
        payload["provider_mutation"] = {
            "name": canonical_name,
            "created": created,
        }
        return payload

    provider_key = _custom_provider_slug(raw_name)
    extra_providers = config.providers.model_extra
    if find_by_name(provider_key):
        provider_key = f"{provider_key}-custom"
    base_provider_key = provider_key
    suffix = 2
    while provider_key in extra_providers:
        provider_key = f"{base_provider_key}-{suffix}"
        suffix += 1

    extra_providers[provider_key] = ProviderConfig(
        label=raw_name,
        api_key=api_key,
        api_base=api_base,
        api_type=api_type,
    )
    save_config(config)
    payload = settings_payload()
    payload["provider_mutation"] = {
        "name": provider_key,
        "created": True,
    }
    return payload


def delete_provider_settings(query: QueryParams) -> dict[str, Any]:
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")

    config = load_config()
    resolved_provider = _resolve_settings_provider(config, provider_name)
    if resolved_provider is None:
        raise WebUISettingsError("unknown provider", status=404)
    spec, provider_key, provider_config = resolved_provider

    extra_providers = config.providers.model_extra or {}

    def matches_provider(value: str | None) -> bool:
        if not value:
            return False
        return value.replace("-", "_") == provider_key.replace("-", "_")

    removed_presets = {
        name
        for name, preset in config.model_presets.items()
        if matches_provider(preset.provider)
    }
    for name in removed_presets:
        del config.model_presets[name]

    defaults = config.agents.defaults
    if defaults.model_preset in removed_presets:
        defaults.model_preset = None
    if matches_provider(defaults.provider):
        defaults.provider = "auto"

    for capability in _STORED_MODEL_CAPABILITIES:
        preset_name = getattr(config.model_defaults, capability)
        if preset_name in removed_presets:
            setattr(
                config.model_defaults,
                capability,
                "default" if capability == "text" else None,
            )

    defaults.fallback_models = [
        fallback
        for fallback in defaults.fallback_models
        if not (
            (isinstance(fallback, str) and fallback in removed_presets)
            or (
                not isinstance(fallback, str)
                and matches_provider(getattr(fallback, "provider", None))
            )
        )
    ]

    if matches_provider(config.transcription.provider):
        config.transcription.enabled = False
        config.transcription.provider = None
        config.transcription.model = None

    if provider_key in extra_providers:
        del extra_providers[provider_key]
    else:
        if spec.is_oauth:
            raise WebUISettingsError("OAuth providers must be disconnected", status=409)
        setattr(config.providers, provider_key, type(provider_config)())
    save_config(config)
    return settings_payload()


def update_provider_settings(query: QueryParams) -> dict[str, Any]:
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")

    config = load_config()
    resolved_provider = _resolve_settings_provider(config, provider_name)
    if resolved_provider is None:
        raise WebUISettingsError("unknown provider")
    spec, provider_key, provider_config = resolved_provider
    if spec.is_oauth:
        raise WebUISettingsError("unknown provider")

    changed = False
    if "label" in query or "displayName" in query:
        label = (_query_first_alias(query, "label", "displayName") or "").strip()
        if not label:
            raise WebUISettingsError("label is required")
        if provider_config.label != label:
            provider_config.label = label
            changed = True

    if "api_key" in query or "apiKey" in query:
        api_key = _query_first_alias(query, "api_key", "apiKey")
        api_key = (api_key or "").strip() or None
        if provider_config.api_key != api_key:
            provider_config.api_key = api_key
            changed = True

    if "api_base" in query or "apiBase" in query:
        api_base = _query_first_alias(query, "api_base", "apiBase")
        api_base = (api_base or "").strip() or None
        if provider_config.api_base != api_base:
            provider_config.api_base = api_base
            changed = True

    if "api_type" in query:
        if spec.name == "openai" or provider_key in (config.providers.model_extra or {}):
            api_type = (_query_first(query, "api_type") or "").strip()
            try:
                parsed_api_type = type(provider_config)(api_type=api_type).api_type
            except Exception:
                raise WebUISettingsError("api_type must be auto, chat_completions, or responses") from None
            if provider_config.api_type != parsed_api_type:
                provider_config.api_type = parsed_api_type
                changed = True

    if changed:
        save_config(config)
    image_config = config.tools.image_generation
    restart_required = (
        changed
        and image_config.enabled
        and image_config.provider == provider_key
        and get_image_gen_provider(provider_key) is not None
    )
    return settings_payload(requires_restart=restart_required)


def login_oauth_provider(query: QueryParams) -> dict[str, Any]:
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")
    spec = find_by_name(provider_name)
    if spec is None or not spec.is_oauth:
        raise WebUISettingsError("unknown OAuth provider")

    if spec.name == "openai_codex":
        try:
            from oauth_cli_kit import get_token, login_oauth_interactive
        except ImportError:
            raise WebUISettingsError("oauth_cli_kit is not installed", status=500) from None

        token = None
        with suppress(Exception):
            token = get_token()
        if not (token and token.access):
            messages: list[str] = []
            token = login_oauth_interactive(
                print_fn=lambda message: messages.append(str(message)),
                prompt_fn=lambda _prompt: "",
            )
        if not (token and token.access):
            raise WebUISettingsError("OAuth login failed", status=401)
        return settings_payload()

    if spec.name == "github_copilot":
        try:
            from nanobot.providers.github_copilot_provider import (
                get_github_copilot_login_status,
                login_github_copilot,
            )
        except ImportError:
            raise WebUISettingsError("GitHub Copilot OAuth support is unavailable", status=500) from None

        token = get_github_copilot_login_status()
        if not token:
            token = login_github_copilot(print_fn=lambda _message: None)
        if not (token and token.access):
            raise WebUISettingsError("OAuth login failed", status=401)
        return settings_payload()

    raise WebUISettingsError("OAuth login is not supported for this provider")


def logout_oauth_provider(query: QueryParams) -> dict[str, Any]:
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")
    spec = find_by_name(provider_name)
    if spec is None or not spec.is_oauth:
        raise WebUISettingsError("unknown OAuth provider")

    if spec.name == "openai_codex":
        try:
            from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
            from oauth_cli_kit.storage import FileTokenStorage
        except ImportError:
            raise WebUISettingsError("oauth_cli_kit is not installed", status=500) from None
        token_path = FileTokenStorage(token_filename=OPENAI_CODEX_PROVIDER.token_filename).get_token_path()
    elif spec.name == "github_copilot":
        try:
            from nanobot.providers.github_copilot_provider import get_storage
        except ImportError:
            raise WebUISettingsError("GitHub Copilot OAuth support is unavailable", status=500) from None
        token_path = get_storage().get_token_path()
    else:
        raise WebUISettingsError("OAuth logout is not supported for this provider")

    for path in (token_path, token_path.with_suffix(".lock")):
        with suppress(FileNotFoundError):
            path.unlink()
    return settings_payload()


def update_network_safety_settings(query: QueryParams) -> dict[str, Any]:
    raw_allow = (
        _query_first_alias(query, "webui_allow_local_service_access", "webuiAllowLocalServiceAccess")
        or _query_first_alias(query, "allow_local_preview_access", "allowLocalPreviewAccess")
    )
    raw_default_access_mode = _query_first_alias(query, "webui_default_access_mode", "webuiDefaultAccessMode")
    if raw_allow is None and raw_default_access_mode is None:
        raise WebUISettingsError("webui_allow_local_service_access or webui_default_access_mode is required")

    config = load_config()
    changed = False
    if raw_allow is not None:
        webui_allow_local_service_access = _parse_bool(raw_allow, "webui_allow_local_service_access")
        if config.tools.webui_allow_local_service_access != webui_allow_local_service_access:
            config.tools.webui_allow_local_service_access = webui_allow_local_service_access
            changed = True

    if changed:
        save_config(config)
    if raw_default_access_mode is not None:
        default_access_mode = raw_default_access_mode.strip().lower()
        if default_access_mode == "restricted":
            default_access_mode = "default"
        if default_access_mode not in {"default", "full"}:
            raise WebUISettingsError("webui_default_access_mode must be default or full")
        try:
            write_webui_default_access_mode(default_access_mode)
        except ValueError as exc:
            raise WebUISettingsError(str(exc)) from exc
    return settings_payload(requires_restart=changed)


def update_web_search_settings(query: QueryParams) -> dict[str, Any]:
    provider_name = (_query_first(query, "provider") or "").strip().lower()
    provider_option = _WEB_SEARCH_PROVIDER_BY_NAME.get(provider_name)
    if provider_option is None:
        raise WebUISettingsError("unknown web search provider")

    config = load_config()
    search_config = config.tools.web.search
    web_config = config.tools.web
    previous_provider = search_config.provider
    changed = False
    restart_required = False

    def set_search_value(attr: str, value: object) -> None:
        nonlocal changed
        if getattr(search_config, attr) != value:
            setattr(search_config, attr, value)
            changed = True

    def set_fetch_value(attr: str, value: object) -> None:
        nonlocal changed
        if getattr(web_config.fetch, attr) != value:
            setattr(web_config.fetch, attr, value)
            changed = True

    if search_config.provider != provider_name:
        search_config.provider = provider_name
        changed = True

    credential = provider_option["credential"]
    if credential == "none":
        set_search_value("api_key", "")
        set_search_value("base_url", "")
    elif credential == "base_url":
        base_url = _query_first_alias(query, "base_url", "baseUrl")
        base_url = base_url.strip() if base_url is not None else None
        if not base_url and previous_provider == provider_name and search_config.base_url:
            base_url = search_config.base_url
        if not base_url:
            raise WebUISettingsError("base_url is required")
        set_search_value("base_url", base_url)
        set_search_value("api_key", "")
    elif credential in {"api_key", "optional_api_key"}:
        raw_api_key = _query_first_alias(query, "api_key", "apiKey")
        api_key = raw_api_key.strip() if raw_api_key is not None else None
        if api_key is None and previous_provider == provider_name and search_config.api_key:
            api_key = search_config.api_key
        if credential == "api_key" and not api_key:
            raise WebUISettingsError("api_key is required")
        set_search_value("api_key", api_key or "")
        set_search_value("base_url", "")
    else:
        raise WebUISettingsError("unknown web search credential type")

    max_results = _query_first_alias(query, "max_results", "maxResults")
    if max_results is not None:
        try:
            parsed = int(max_results)
        except ValueError:
            raise WebUISettingsError("max_results must be an integer") from None
        if parsed < 1 or parsed > 10:
            raise WebUISettingsError("max_results must be between 1 and 10")
        set_search_value("max_results", parsed)

    timeout = _query_first(query, "timeout")
    if timeout is not None:
        try:
            parsed_timeout = int(timeout)
        except ValueError:
            raise WebUISettingsError("timeout must be an integer") from None
        if parsed_timeout < 1 or parsed_timeout > 120:
            raise WebUISettingsError("timeout must be between 1 and 120")
        set_search_value("timeout", parsed_timeout)

    use_jina_reader = _query_first_alias(query, "use_jina_reader", "useJinaReader")
    if use_jina_reader is not None:
        normalized = use_jina_reader.strip().lower()
        if normalized not in {"1", "0", "true", "false", "yes", "no"}:
            raise WebUISettingsError("use_jina_reader must be boolean")
        previous_jina_reader = web_config.fetch.use_jina_reader
        set_fetch_value("use_jina_reader", normalized in {"1", "true", "yes"})
        if web_config.fetch.use_jina_reader != previous_jina_reader:
            restart_required = True

    if changed:
        save_config(config)
    return settings_payload(requires_restart=restart_required)


def update_image_generation_settings(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    image_config = config.tools.image_generation
    changed = False

    provider_name = _query_first(query, "provider")
    if provider_name is not None:
        provider_name = provider_name.strip().lower()
        if not provider_name:
            raise WebUISettingsError("image generation provider is required")
        if get_image_gen_provider(provider_name) is None:
            raise WebUISettingsError("unknown image generation provider")
        if image_config.provider != provider_name:
            image_config.provider = provider_name
            changed = True

    enabled = _query_first(query, "enabled")
    if enabled is not None:
        parsed_enabled = _parse_bool(enabled, "enabled")
        if image_config.enabled != parsed_enabled:
            image_config.enabled = parsed_enabled
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("image generation model is required")
        if len(model) > 200:
            raise WebUISettingsError("image generation model is too long")
        if image_config.model != model:
            image_config.model = model
            changed = True

    default_aspect_ratio = _query_first_alias(
        query,
        "default_aspect_ratio",
        "defaultAspectRatio",
    )
    if default_aspect_ratio is not None:
        default_aspect_ratio = default_aspect_ratio.strip()
        if default_aspect_ratio not in _IMAGE_GENERATION_ASPECT_RATIOS:
            raise WebUISettingsError("unsupported image generation aspect ratio")
        if image_config.default_aspect_ratio != default_aspect_ratio:
            image_config.default_aspect_ratio = default_aspect_ratio
            changed = True

    default_image_size = _query_first_alias(
        query,
        "default_image_size",
        "defaultImageSize",
    )
    if default_image_size is not None:
        default_image_size = default_image_size.strip()
        if not default_image_size:
            raise WebUISettingsError("default image size is required")
        if len(default_image_size) > 32 or not all(
            char.isascii() and (char.isalnum() or char in {"x", "X", ":", "-", "_"})
            for char in default_image_size
        ):
            raise WebUISettingsError("unsupported image generation size")
        if image_config.default_image_size != default_image_size:
            image_config.default_image_size = default_image_size
            changed = True

    max_images_per_turn = _query_first_alias(
        query,
        "max_images_per_turn",
        "maxImagesPerTurn",
    )
    if max_images_per_turn is not None:
        try:
            parsed_max = int(max_images_per_turn)
        except ValueError:
            raise WebUISettingsError("max_images_per_turn must be an integer") from None
        if parsed_max < 1 or parsed_max > 8:
            raise WebUISettingsError("max_images_per_turn must be between 1 and 8")
        if image_config.max_images_per_turn != parsed_max:
            image_config.max_images_per_turn = parsed_max
            changed = True

    if image_config.enabled:
        selected_provider = next(
            (
                provider
                for provider in _image_generation_provider_rows(config)
                if provider["name"] == image_config.provider
            ),
            None,
        )
        if not selected_provider or not selected_provider["configured"]:
            raise WebUISettingsError("image generation provider is not configured")

    if changed:
        save_config(config)
    return settings_payload(requires_restart=changed)


def update_transcription_settings(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    transcription = config.transcription
    changed = False

    enabled = _query_first(query, "enabled")
    if enabled is not None:
        parsed_enabled = _parse_bool(enabled, "enabled")
        if transcription.enabled != parsed_enabled:
            transcription.enabled = parsed_enabled
            changed = True

    provider = _query_first(query, "provider")
    if provider is not None:
        provider = provider.strip().lower()
        provider_spec = resolve_transcription_provider(provider)
        if provider_spec is None:
            raise WebUISettingsError("unknown transcription provider")
        provider = provider_spec.name
        if transcription.provider != provider:
            transcription.provider = provider
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip() or None
        if model is not None and len(model) > 200:
            raise WebUISettingsError("transcription model is too long")
        if transcription.model != model:
            transcription.model = model
            changed = True

    language = _query_first(query, "language")
    if language is not None:
        language = language.strip().lower() or None
        if language is not None and not re.fullmatch(r"[a-z]{2,3}", language):
            raise WebUISettingsError("transcription language must be 2-3 lowercase letters")
        if transcription.language != language:
            transcription.language = language
            changed = True

    max_duration_sec = _query_first_alias(query, "max_duration_sec", "maxDurationSec")
    if max_duration_sec is not None:
        try:
            parsed_duration = int(max_duration_sec)
        except ValueError:
            raise WebUISettingsError("max_duration_sec must be an integer") from None
        if parsed_duration < 1 or parsed_duration > 600:
            raise WebUISettingsError("max_duration_sec must be between 1 and 600")
        if transcription.max_duration_sec != parsed_duration:
            transcription.max_duration_sec = parsed_duration
            changed = True

    max_upload_mb = _query_first_alias(query, "max_upload_mb", "maxUploadMb")
    if max_upload_mb is not None:
        try:
            parsed_upload = int(max_upload_mb)
        except ValueError:
            raise WebUISettingsError("max_upload_mb must be an integer") from None
        if parsed_upload < 1 or parsed_upload > 100:
            raise WebUISettingsError("max_upload_mb must be between 1 and 100")
        if transcription.max_upload_mb != parsed_upload:
            transcription.max_upload_mb = parsed_upload
            changed = True

    if changed:
        save_config(config)
    return settings_payload()
