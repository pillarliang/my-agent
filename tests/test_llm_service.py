"""模型服务检查：内部配置、统一调用与显式关闭。"""

import os

import pytest
from protocol_support import completion, local_api

from learn_agent.models import LLMService, ModelError, ModelRequest, TextMessage


@pytest.fixture(autouse=True)
def isolated_model_environment():
    # 每个案例使用临时配置，不读取真实凭证，也不依赖开发者的模型环境变量。
    names = (
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_BASE_URL",
        "DASHSCOPE_MODEL",
        "DASHSCOPE_USE_ENV_PROXY",
    )
    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    yield
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def write_config(path, endpoint):
    path.write_text(
        f"DASHSCOPE_API_KEY=local-config-test\nDASHSCOPE_BASE_URL={endpoint}\n"
        "DASHSCOPE_MODEL=file-model\nDASHSCOPE_USE_ENV_PROXY=false\n",
        encoding="utf-8",
    )


def test_service_loads_configuration_reuses_client_and_closes(tmp_path):
    config = tmp_path / ".env"
    request = ModelRequest((TextMessage("user", "你好"),))
    with local_api([completion(text="配置生效")]) as (client, requests):
        write_config(config, client.base_url)
        service = LLMService(env_path=config)
        try:
            assert service.invoke(request).text == "配置生效"
            assert service.invoke(request).text == "配置生效"
        finally:
            service.close()
        service.close()  # 多次关闭不应报错，也不允许重建 client 后继续请求。
        with pytest.raises(ModelError) as caught:
            service.invoke(request)
    assert caught.value.code == "closed"
    assert len(requests) == 2
    assert all(item["model"] == "file-model" for item in requests)
    assert requests[0]["enable_thinking"] is False


def test_service_closes_on_caller_failure(tmp_path):
    config = tmp_path / ".env"
    request = ModelRequest((TextMessage("user", "你好"),))
    with local_api([completion(text="已连接")]) as (client, requests):
        write_config(config, client.base_url)
        service = LLMService(env_path=config)
        with pytest.raises(ValueError, match="调用方失败"):
            try:
                assert service.invoke(request).text == "已连接"
                raise ValueError("调用方失败")
            finally:
                service.close()
        with pytest.raises(ModelError) as caught:
            service.invoke(request)
    assert caught.value.code == "closed"
    assert len(requests) == 1


def test_existing_environment_takes_precedence(tmp_path, monkeypatch):
    config = tmp_path / ".env"
    # 文件端点故意不可用；现有环境变量必须优先，不能误用文件配置。
    write_config(config, "http://127.0.0.1:1/v1")
    with local_api([completion(text="环境优先")]) as (client, requests):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "local-environment-test")
        monkeypatch.setenv("DASHSCOPE_BASE_URL", str(client.base_url))
        monkeypatch.setenv("DASHSCOPE_MODEL", "environment-model")
        service = LLMService(env_path=config)
        try:
            assert service.invoke(ModelRequest((TextMessage("user", "你好"),))).text == "环境优先"
        finally:
            service.close()
    assert requests[0]["model"] == "environment-model"


@pytest.mark.parametrize("missing", ["DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL"])
def test_missing_credentials_fail_inside_service(tmp_path, monkeypatch, missing):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "local-test-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.delenv(missing)
    with pytest.raises(ModelError) as caught:
        LLMService(env_path=tmp_path / "absent.env")
    assert caught.value.code == "configuration_error"
    assert "local-test-key" not in str(caught.value)


def test_invalid_proxy_setting_fails_inside_service(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "local-test-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("DASHSCOPE_USE_ENV_PROXY", "invalid")
    with pytest.raises(ModelError, match="只能是 true 或 false") as caught:
        LLMService(env_path=tmp_path / "absent.env")
    assert caught.value.code == "configuration_error"
