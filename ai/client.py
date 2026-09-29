"""
AI co-author and LLM streaming client interface.
"""

from abc import ABC, abstractmethod
import asyncio
import os
from typing import AsyncIterator
import httpx


class BaseLLMClient(ABC):
    """Abstract LLM Client interface."""

    @abstractmethod
    def stream_completion(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 1000,
    ) -> AsyncIterator[str]:
        """Stream generated completion token by token."""
        raise NotImplementedError

    @abstractmethod
    async def generate_text(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 1000,
    ) -> str:
        """Generate full text completion."""
        pass


class FakeLLMClient(BaseLLMClient):
    """
    Deterministic mock LLM client for testing concurrency and failure scenarios
    without external network dependencies.
    """

    def __init__(self, canned_response: str = "This is a polished and concise rewritten text.") -> None:
        self.canned_response = canned_response
        self.stream_delay = 0.01  # Delay between token chunks

    async def stream_completion(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 1000,
    ) -> AsyncIterator[str]:
        words = self.canned_response.split(" ")
        for i, word in enumerate(words):
            chunk = word if i == 0 else " " + word
            await asyncio.sleep(self.stream_delay)
            yield chunk

    async def generate_text(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 1000,
    ) -> str:
        return self.canned_response


class OpenAICompatibleLLMClient(BaseLLMClient):
    """
    Generic HTTP client supporting OpenAI / Claude / Local LLM completions.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
    ) -> None:
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.base_url = os.environ.get("LLM_BASE_URL", base_url)
        self.model = os.environ.get("LLM_MODEL", model)

    async def stream_completion(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 1000,
    ) -> AsyncIterator[str]:
        if not self.api_key:
            # Fall back to fake client if no API key is provided
            fake = FakeLLMClient()
            async for chunk in fake.stream_completion(prompt, system_prompt, max_tokens):
                yield chunk
            return

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions", headers=headers, json=payload
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                        import json
                        try:
                            data = json.loads(line[6:])
                            delta = data["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield delta
                        except Exception:
                            continue

    async def generate_text(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 1000,
    ) -> str:
        chunks: list[str] = []
        async for chunk in self.stream_completion(prompt, system_prompt, max_tokens):
            chunks.append(chunk)
        return "".join(chunks)
