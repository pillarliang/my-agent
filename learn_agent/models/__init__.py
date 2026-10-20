"""统一模型接口与当前 Chat Completions 实现；路由可在此接口后继续扩展。"""

from .contracts import (
    Model,
    ModelError,
    ModelMessage,
    ModelProtocolError,
    ModelRequest,
    ModelResponse,
    ProviderContinuation,
    TextMessage,
    TokenUsage,
)
from .openai_chat import OpenAIChatModel
from .service import LLMService

__all__ = [
    "LLMService",
    "Model",
    "ModelError",
    "ModelMessage",
    "ModelProtocolError",
    "ModelResponse",
    "ModelRequest",
    "OpenAIChatModel",
    "ProviderContinuation",
    "TextMessage",
    "TokenUsage",
]
