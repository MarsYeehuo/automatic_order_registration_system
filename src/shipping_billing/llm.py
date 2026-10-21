from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate

from .config import LLMCredentials, LLMSettings
from .utils import write_json


@dataclass
class LLMResponse:
    text: str | None
    data: dict[str, Any] | list[Any] | None
    error: str | None = None
    cached: bool = False
    latency_seconds: float | None = None
    model: str | None = None


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" or "text" in block:
                    value = block.get("text")
                    if isinstance(value, str):
                        parts.append(value)
            elif hasattr(block, "text"):
                value = getattr(block, "text")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    return str(content or "")


def _load_json_loose(snippet: str) -> dict[str, Any] | list[Any] | None:
    attempts = [snippet]
    cleaned = re.sub(r"(?<!\\)\r?\n", " ", snippet)
    cleaned = re.sub(r"(?<!\\)\t", " ", cleaned)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    attempts.append(cleaned)
    for attempt in attempts:
        try:
            parsed = json.loads(attempt)
            if isinstance(parsed, (dict, list)):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def parse_json_payload(text: str | None) -> dict[str, Any] | list[Any] | None:
    if not text:
        return None
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            parsed = _load_json_loose(candidate[start : end + 1])
            if parsed is not None:
                return parsed
    return _load_json_loose(candidate)


class LLMClient:
    def __init__(
        self,
        settings: LLMSettings,
        credentials: LLMCredentials,
        prompt_version: str = "1.0.0",
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.prompt_version = prompt_version
        self._model = None
        self._init_error: str | None = None
        self._init()

    def _init(self) -> None:
        if not self.settings.enabled:
            self._init_error = "LLM disabled by configuration"
            return
        if not self.credentials.api_key:
            self._init_error = "未找到 LLM 密钥（template_key.txt）"
            return
        if not self.credentials.model:
            self._init_error = "未配置 LLM 模型名"
            return
        try:
            if self.credentials.provider == "openai":
                from langchain_openai import ChatOpenAI

                kwargs: dict[str, Any] = {
                    "model": self.credentials.model,
                    "api_key": self.credentials.api_key,
                    "temperature": self.settings.temperature,
                    "max_tokens": self.settings.max_tokens,
                    "timeout": self.settings.timeout_seconds,
                }
                if self.credentials.base_url:
                    kwargs["base_url"] = self.credentials.base_url
                self._model = ChatOpenAI(**kwargs)
            else:
                from langchain_anthropic import ChatAnthropic

                kwargs = {
                    "model": self.credentials.model,
                    "anthropic_api_key": self.credentials.api_key,
                    "temperature": self.settings.temperature,
                    "max_tokens": self.settings.max_tokens,
                    "timeout": self.settings.timeout_seconds,
                }
                if self.credentials.base_url:
                    kwargs["anthropic_api_url"] = self.credentials.base_url
                self._model = ChatAnthropic(**kwargs)
        except Exception as exc:  # pragma: no cover - depends on local SDK versions
            self._init_error = f"LLM 初始化失败: {type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._model is not None

    @property
    def disabled_reason(self) -> str | None:
        return self._init_error

    @property
    def model_name(self) -> str | None:
        return self.credentials.model

    @property
    def provider(self) -> str:
        return self.credentials.provider

    def complete_json(
        self,
        system: str,
        user: str,
        cache_tag: str = "generic",
    ) -> LLMResponse:
        if not self.available:
            return LLMResponse(text=None, data=None, error=self._init_error)

        cache_dir = Path(self.settings.cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(
            "\n".join(
                [
                    self.credentials.provider,
                    self.credentials.model or "",
                    self.prompt_version,
                    cache_tag,
                    system,
                    user,
                ]
            ).encode("utf-8")
        ).hexdigest()
        cache_path = cache_dir / f"{digest}.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("ok"):
                    return LLMResponse(
                        text=cached.get("text"),
                        data=cached.get("data"),
                        cached=True,
                        model=self.credentials.model,
                    )
            except (OSError, json.JSONDecodeError):
                pass

        prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessage(content=system),
                ("human", "{payload}"),
            ]
        )
        chain = prompt | self._model
        last_error: str | None = None
        started = time.time()
        for attempt in range(max(1, self.settings.retries + 1)):
            try:
                message = chain.invoke({"payload": user})
                text = _extract_text(message.content)
                data = parse_json_payload(text)
                if data is None:
                    last_error = "LLM 输出不是合法 JSON"
                    user = (
                        user
                        + "\n\n上一次输出无法解析为 JSON。请仅输出一个合法 JSON 对象，不要输出解释或 Markdown。"
                    )
                    continue
                latency = time.time() - started
                if isinstance(data, dict):
                    write_json(
                        cache_path,
                        {
                            "ok": True,
                            "text": text,
                            "data": data,
                            "model": self.credentials.model,
                            "prompt_version": self.prompt_version,
                        },
                    )
                return LLMResponse(
                    text=text,
                    data=data,
                    latency_seconds=latency,
                    model=self.credentials.model,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
        return LLMResponse(
            text=None,
            data=None,
            error=last_error,
            latency_seconds=time.time() - started,
            model=self.credentials.model,
        )
