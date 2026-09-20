"""Package source settings and explicit connectivity checks."""

from nanobot.config.loader import load_config, save_config
from nanobot.runtime.dependencies import (
    check_package_sources,
    dependency_status,
    normalize_source_url,
)


def package_sources_payload() -> dict:
    load_config()
    return dependency_status()


def save_package_sources(values: dict) -> dict:
    config = load_config()
    current = config.tools.package_sources.model_dump()
    allowed = {"enabled", "npm_registry", "pypi_index_url", "workspace_python"}
    if not values or set(values) - allowed:
        raise ValueError("Unknown or empty package source settings")
    for key in ("enabled", "workspace_python"):
        if key in values and type(values[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    for key in ("npm_registry", "pypi_index_url"):
        if key in values:
            if not isinstance(values[key], str):
                raise ValueError(f"{key} must be a URL")
            values[key] = normalize_source_url(values[key], pypi=key == "pypi_index_url")
    updated = type(config.tools.package_sources).model_validate({**current, **values})
    config.tools.package_sources = updated
    save_config(config)
    return package_sources_payload()


async def check_package_source_settings() -> dict:
    payload = package_sources_payload()
    return {**payload, "checks": await check_package_sources(force=True)}
