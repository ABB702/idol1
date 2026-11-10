import asyncio
import json
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx

from .db import Database


class ModelError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, db: Database, api_key: str = ""):
        self.db = db
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), follow_redirects=False)
        self.limit = asyncio.Semaphore(2)

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    @property
    def model(self) -> str:
        return self.db.settings()["model"]

    @property
    def evaluation_model(self) -> str:
        return self.db.settings()["evaluation_model"] or self.model

    def _url(self, suffix: str) -> str:
        base = self.db.settings()["base_url"].rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ModelError("模型服务地址须使用 HTTPS；本机服务可使用 HTTP。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelError("模型服务地址不能包含账号、查询参数或片段。")
        return base + suffix

    def _headers(self) -> dict:
        if not self.configured:
            raise ModelError("请先在设置中填写 API Key 并测试连接。")
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _check(response: httpx.Response):
        if response.is_error or response.is_redirect:
            # Never echo provider payloads: they can contain credentials or private request content.
            code = response.status_code
            detail = {401: "API Key 无效", 402: "服务余额不足", 403: "服务拒绝访问", 429: "请求过于频繁"}.get(
                code, "请检查模型名称、服务地址和网络"
            )
            raise ModelError(f"模型服务返回 HTTP {code}：{detail}。")

    async def complete(self, messages: list[dict], *, json_mode=False, model=None) -> str:
        payload = {"model": model or self.model, "messages": messages, "stream": False, "max_tokens": 4096}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with self.limit:
                response = await self.client.post(
                    self._url("/chat/completions"), headers=self._headers(), json=payload
                )
                self._check(response)
                choice = response.json()["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ModelError("回答达到模型输出长度上限，请缩小问题范围。")
                content = choice["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ModelError("模型没有返回可用的回答。")
                return content
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise ModelError("模型调用失败，请检查网络和服务配置后重试。") from exc

    async def stream(self, messages: list[dict], *, model=None) -> AsyncIterator[str]:
        payload = {"model": model or self.model, "messages": messages, "stream": True, "max_tokens": 4096}
        try:
            completed = False
            async with (
                self.limit,
                self.client.stream(
                    "POST", self._url("/chat/completions"), headers=self._headers(), json=payload
                ) as response,
            ):
                self._check(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        completed = True
                        break
                    if not raw:
                        continue
                    event = json.loads(raw)
                    if "error" in event:
                        raise ModelError("模型服务中断了回答，请重试。")
                    for choice in event.get("choices", []):
                        if choice.get("finish_reason") == "length":
                            raise ModelError("回答达到输出长度上限，请缩小问题范围后重试。")
                        if choice.get("finish_reason") in {"stop", "tool_calls"}:
                            completed = True
                        text = choice.get("delta", {}).get("content")
                        if isinstance(text, str) and text:
                            yield text
            if not completed:
                raise ModelError("模型连接提前结束，回答可能不完整，请重试。")
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelError("模型流式连接中断，请检查网络后重试。") from exc

    async def test(self) -> dict:
        try:
            async with self.limit:
                response = await self.client.get(self._url("/models"), headers=self._headers())
                self._check(response)
                models = [
                    item["id"] for item in response.json().get("data", []) if isinstance(item.get("id"), str)
                ]
            return {"ok": True, "models": models, "message": "连接成功，请选择服务支持的模型。"}
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelError("连接测试失败，请检查网络和服务地址。") from exc

    async def close(self):
        await self.client.aclose()
