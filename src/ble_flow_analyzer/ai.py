from __future__ import annotations

from dataclasses import dataclass
import json
import os
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .domain import BleEvent
from .knowledge import KnowledgeEntry


DEFAULT_OLLAMA_URL = "http://localhost:11434"
PREFERRED_MODELS = ("qwen2.5:1.5b", "qwen2.5:0.5b")
PROVIDERS = {
    "ollama": {
        "label": "本地 Ollama",
        "url": DEFAULT_OLLAMA_URL,
        "env": "",
        "models": (),
    },
    "qwen": {
        "label": "通义千问（阿里云百炼）",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env": "DASHSCOPE_API_KEY",
        "models": ("qwen3.8-flash", "qwen3.7-plus", "qwen-plus", "qwen-turbo"),
    },
    "deepseek": {
        "label": "DeepSeek",
        "url": "https://api.deepseek.com",
        "env": "DEEPSEEK_API_KEY",
        "models": ("deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"),
    },
}

SYSTEM_PROMPT = """你是 BLE 协议学习助手。请使用中文简洁回答，并严格遵守：
1. 区分 API 实际数据、协议推断和理论协议结构。
2. 不得把理论 ATT/LL/L2CAP 帧说成实际捕获帧。
3. 未提供的数据必须明确说明不可用，不得虚构 Handle、Opcode、错误码或 HEX。
4. 优先依据用户提供的当前事件和本地知识内容回答。
5. 如有推测，必须明确标记为推测。"""


class OllamaError(RuntimeError):
    pass


class AiError(OllamaError):
    pass


def environment_key(provider: str) -> str:
    env_name = str(PROVIDERS[provider]["env"])
    return os.environ.get(env_name, "") if env_name else ""


@dataclass(frozen=True, slots=True)
class AiClient:
    provider: str
    api_key: str = ""
    timeout_seconds: float = 90.0

    def list_ollama_models(self) -> list[str]:
        return OllamaClient(timeout_seconds=5).list_models()

    def stream_chat(self, model: str, question: str, context: str) -> Iterator[str]:
        if self.provider == "ollama":
            yield from OllamaClient(timeout_seconds=self.timeout_seconds).stream_chat(model, question, context)
            return
        provider = PROVIDERS.get(self.provider)
        if provider is None:
            raise AiError(f"未知的 AI 提供商：{self.provider}")
        if not self.api_key:
            raise AiError(f"请填写 API Key 或设置环境变量 {provider['env']}。")
        yield from self._stream_openai_compatible(model, question, context, provider)

    def _stream_openai_compatible(
        self,
        model: str,
        question: str,
        context: str,
        provider: dict[str, object],
    ) -> Iterator[str]:
        payload = {
            "model": model,
            "stream": True,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"{context}\n\n用户问题：{question}"},
            ],
        }
        request = Request(
            f"{str(provider['url']).rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    choices = json.loads(data).get("choices", [])
                    if choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta", {})
                        if isinstance(delta, dict) and delta.get("content"):
                            yield str(delta["content"])
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise AiError(f"{provider['label']} 请求失败（HTTP {error.code}）：{detail}") from error
        except URLError as error:
            raise AiError(f"无法连接 {provider['label']}：{error.reason}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AiError(f"{provider['label']} 返回了无法解析的数据。") from error


@dataclass(frozen=True, slots=True)
class OllamaClient:
    base_url: str = DEFAULT_OLLAMA_URL
    timeout_seconds: float = 60.0

    def list_models(self) -> list[str]:
        payload = self._request("GET", "/api/tags")
        return [
            str(model["name"])
            for model in payload.get("models", [])
            if isinstance(model, dict) and model.get("name")
        ]

    def chat(self, model: str, question: str, context: str) -> str:
        payload = self._request(
            "POST",
            "/api/chat",
            {
                "model": model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"{context}\n\n用户问题：{question}"},
                ],
            },
        )
        message = payload.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if not content:
            raise OllamaError("Ollama 未返回回答内容。")
        return str(content).strip()

    def stream_chat(self, model: str, question: str, context: str) -> Iterator[str]:
        body = {
            "model": model,
            "stream": True,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"{context}\n\n用户问题：{question}"},
            ],
        }
        request = Request(
            f"{self.base_url.rstrip('/')}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                for line in response:
                    if not line.strip():
                        continue
                    payload = json.loads(line.decode("utf-8"))
                    message = payload.get("message", {})
                    chunk = message.get("content") if isinstance(message, dict) else None
                    if chunk:
                        yield str(chunk)
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise OllamaError(f"Ollama 请求失败（HTTP {error.code}）：{detail}") from error
        except URLError as error:
            raise OllamaError(f"无法连接 Ollama：{error.reason}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise OllamaError("Ollama 返回了无法解析的数据。") from error

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise OllamaError(f"Ollama 请求失败（HTTP {error.code}）：{detail}") from error
        except URLError as error:
            raise OllamaError(f"无法连接 Ollama：{error.reason}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise OllamaError("Ollama 返回了无法解析的数据。") from error
        if not isinstance(result, dict):
            raise OllamaError("Ollama 返回格式不正确。")
        return result


def choose_default_model(models: list[str]) -> str:
    for preferred in PREFERRED_MODELS:
        if preferred in models:
            return preferred
    return models[0] if models else ""


def build_event_context(
    event: BleEvent | None,
    knowledge: KnowledgeEntry | None,
    *,
    include_event: bool,
    include_raw_data: bool,
    include_knowledge: bool,
) -> str:
    sections = ["以下内容来自 BLE Flow Analyzer，请注意每部分的数据来源。"]
    if event is None:
        sections.append("当前没有选中的流程事件。")
    else:
        if include_event:
            fields = "\n".join(f"- {key}: {value}" for key, value in event.fields.items()) or "- 无"
            sections.append(
                "[当前事件]\n"
                f"类型: {event.packet_type.value}\n"
                f"证据: {event.evidence.value}\n"
                f"方向: {event.direction}\n"
                f"摘要: {event.summary}\n"
                f"设备: {event.device_id or '无'}\n"
                f"字段:\n{fields}"
            )
        if include_raw_data:
            raw_data = event.raw_data.hex(" ").upper() if event.raw_data else "不可用"
            sections.append(f"[API/事件携带的原始数据]\nHEX: {raw_data}")
    if include_knowledge:
        if knowledge is None:
            sections.append("[本地协议知识]\n当前事件没有对应知识条目。")
        else:
            sections.append(
                "[本地协议知识，可能包含理论结构]\n"
                f"说明: {knowledge.description}\n"
                f"概览: {knowledge.overview or '无'}\n"
                f"协议结构: {knowledge.structure_note or '无'}\n"
                f"数据边界: {knowledge.data_note or '无'}"
            )
    return "\n\n".join(sections)