"""模型服务：在类内部管理配置、凭证、SDK client 和连接生命周期。"""

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import DefaultHttpxClient, OpenAI

from .contracts import ModelError, ModelRequest, ModelResponse
from .openai_chat import OpenAIChatModel

ROOT = Path(__file__).resolve().parents[2]


class LLMService:
    """应用创建服务，Runner 只调用 invoke；凭证和 SDK 对象不穿过接口。"""

    def __init__(self, *, env_path: Path | None = None) -> None:
        load_dotenv(
            env_path if env_path is not None else ROOT / ".env", override=False, interpolate=False
        )
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        base_url = os.environ.get("DASHSCOPE_BASE_URL")
        if not api_key or not base_url:
            raise ModelError(
                "configuration_error", "请在 .env 中配置 DASHSCOPE_API_KEY 和 DASHSCOPE_BASE_URL。"
            )
        proxy = os.environ.get("DASHSCOPE_USE_ENV_PROXY", "true").lower()
        if proxy not in {"true", "false"}:
            raise ModelError(
                "configuration_error", "DASHSCOPE_USE_ENV_PROXY 只能是 true 或 false。"
            )
        self._api_key = api_key
        self._base_url = base_url
        self._model_name = os.environ.get("DASHSCOPE_MODEL", "qwen3.8-flash")
        # 一个服务实例复用一个连接池；一次 invoke 只进行一次网络尝试。
        self._client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
            timeout=15,
            http_client=DefaultHttpxClient(trust_env=proxy == "true"),
        )
        self._adapter = OpenAIChatModel(
            self._client, self._model_name, extra_body={"enable_thinking": False}
        )

    def invoke(self, request: ModelRequest) -> ModelResponse:
        """统一模型调用入口；不执行工具，不控制 Agent Loop。"""
        if self._client.is_closed():
            raise ModelError("closed", "模型服务已关闭。")
        return self._adapter.invoke(request)

    def close(self) -> None:
        """释放服务内部的连接；由应用在退出时调用，可以重复关闭。"""
        self._client.close()
