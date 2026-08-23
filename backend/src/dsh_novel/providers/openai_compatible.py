from __future__ import annotations

import httpx

from dsh_novel.providers.base import WriterRequest


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None,
        timeout_seconds: float,
        max_output_tokens: int,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens

    def generate_chapter(self, request: WriterRequest) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        context_text = "\n\n".join(
            f"[{block.kind}]\n{block.content}" for block in request.context.blocks
        )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是小说正文写作者。只输出本章正文，不输出分析、系统说明、JSON、"
                        "提示词标签或审稿意见。严格遵循章节合同，不重复已有段落。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"小说：{request.project_title}\n"
                        f"目标字数：约{request.contract.target_words}字\n\n{context_text}"
                    ),
                },
            ],
            "temperature": 0.8,
            "max_tokens": self.max_output_tokens,
        }
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.endpoint}/chat/completions", headers=headers, json=payload
                )
                response.raise_for_status()
                data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(f"model request failed: {exc}") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("model returned empty chapter content")
        return content.strip()
