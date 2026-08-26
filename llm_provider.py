from __future__ import annotations

import json
import os
from typing import Any, Protocol

from dotenv import load_dotenv
from openai import OpenAI

import database


SAFE_PROVIDER_MESSAGE = "模型暂时没有返回有效结果，请稍后重试。"


class ProviderError(RuntimeError):
    """Safe user-facing failure without request data or raw exception details."""


class RateLimitError(ProviderError):
    pass


class LLMProvider(Protocol):
    provider_name: str

    def is_configured(self) -> bool: ...

    def json_call(
        self, system_prompt: str, user_payload: dict[str, Any], *,
        user_id: str = database.DEV_USER_ID, operation: str = "unknown",
        client: Any | None = None,
    ) -> dict: ...


class OpenAICompatibleProvider:
    provider_name = "compatible"
    api_key_env = ""
    base_url_env = ""
    model_env = ""
    default_base_url = ""
    use_response_format = True

    def __init__(self, api_key: str | None = None) -> None:
        # Real server/container environment wins; .env only fills missing values.
        load_dotenv(override=False)
        self.api_key = (api_key or os.getenv(self.api_key_env, "")).strip()
        self.base_url = os.getenv(self.base_url_env, self.default_base_url).strip()
        self.model = os.getenv(self.model_env, "").strip()

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def create_client(self) -> OpenAI:
        if not self.is_configured():
            raise ProviderError(f"尚未完整配置 {self.provider_name} API。请检查服务器环境变量。")
        timeout = max(1, int(os.getenv("LLM_TIMEOUT_SECONDS", "90")))
        return OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=timeout, max_retries=0)

    def _request_kwargs(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "请按要求输出 json。输入：\n" + json.dumps(user_payload, ensure_ascii=False)},
            ],
            "temperature": 0.1,
        }
        if self.use_response_format:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def json_call(
        self, system_prompt: str, user_payload: dict[str, Any], *,
        user_id: str = database.DEV_USER_ID, operation: str = "unknown",
        client: Any | None = None,
    ) -> dict:
        operation_id = database.start_llm_operation(user_id, self.provider_name, operation)
        try:
            active_client = client or self.create_client()
            retries = max(0, int(os.getenv("MAX_LLM_RETRIES", "1")))
            for attempt in range(1, retries + 2):
                try:
                    usage_id = database.reserve_llm_request(
                        user_id, operation_id, self.provider_name, operation, attempt
                    )
                except PermissionError as exc:
                    raise RateLimitError(str(exc)) from exc
                try:
                    response = active_client.chat.completions.create(
                        **self._request_kwargs(system_prompt, user_payload)
                    )
                    content = response.choices[0].message.content
                    if not content or not str(content).strip():
                        raise ValueError("empty_response")
                    result = json.loads(content)
                    if not isinstance(result, dict):
                        raise ValueError("invalid_json_shape")
                    database.finish_llm_usage(usage_id, user_id, "success")
                    database.finish_llm_operation(operation_id, user_id, "success")
                    return result
                except Exception as exc:
                    code = "invalid_response" if isinstance(exc, (ValueError, json.JSONDecodeError)) else "provider_error"
                    database.finish_llm_usage(usage_id, user_id, "failure", code)
                    if attempt > retries:
                        raise ProviderError(SAFE_PROVIDER_MESSAGE) from exc
            raise ProviderError(SAFE_PROVIDER_MESSAGE)
        except RateLimitError:
            database.finish_llm_operation(operation_id, user_id, "blocked", "daily_limit")
            raise
        except ProviderError:
            database.finish_llm_operation(operation_id, user_id, "failure", "provider_error")
            raise
        except Exception as exc:
            database.finish_llm_operation(operation_id, user_id, "failure", "provider_error")
            raise ProviderError(SAFE_PROVIDER_MESSAGE) from exc


class DeepSeekProvider(OpenAICompatibleProvider):
    provider_name = "deepseek"
    api_key_env = "DEEPSEEK_API_KEY"
    base_url_env = "DEEPSEEK_BASE_URL"
    model_env = "DEEPSEEK_MODEL"
    default_base_url = "https://api.deepseek.com"


class QwenProvider(OpenAICompatibleProvider):
    provider_name = "qwen"
    api_key_env = "QWEN_API_KEY"
    base_url_env = "QWEN_BASE_URL"
    model_env = "QWEN_MODEL"

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(api_key=api_key)
        self.use_response_format = os.getenv("QWEN_RESPONSE_FORMAT", "json_object").strip().lower() == "json_object"


def get_provider(api_key: str | None = None) -> LLMProvider:
    provider_name = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
    if provider_name == "deepseek":
        return DeepSeekProvider(api_key=api_key)
    if provider_name in {"qwen", "aliyun", "dashscope"}:
        return QwenProvider(api_key=api_key)
    raise ProviderError(f"不支持的 LLM Provider：{provider_name}")
