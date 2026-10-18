"""Local environment loading, separate from model and application behavior."""

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv

Provider = Literal["dashscope", "openai"]
DEFAULT_MODEL = "qwen3.8-flash"


def resolve_model(provider: Provider, model: str | None = None) -> str:
    if provider not in ("dashscope", "openai"):
        raise ValueError("Provider must be dashscope or openai.")
    variable = "DASHSCOPE_MODEL" if provider == "dashscope" else "OPENAI_MODEL"
    fallback = DEFAULT_MODEL if provider == "dashscope" else ""
    resolved = (model if model is not None else os.environ.get(variable, fallback)).strip()
    if not resolved:
        raise ValueError(f"Set {variable} or pass --model.")
    return resolved


def load_environment(path: Path) -> None:
    if path.is_file():
        # Explicit file only: do not search parents or evaluate shell expressions.
        load_dotenv(path, override=False, interpolate=False)


def dashscope_endpoint() -> str:
    endpoint = os.environ.get("DASHSCOPE_BASE_URL", "").rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Set DASHSCOPE_BASE_URL to an HTTPS endpoint without credentials or query."
        )
    return endpoint


def dashscope_use_env_proxy() -> bool:
    value = os.environ.get("DASHSCOPE_USE_ENV_PROXY", "true").lower()
    if value not in ("true", "false"):
        raise ValueError("DASHSCOPE_USE_ENV_PROXY must be true or false.")
    return value == "true"
