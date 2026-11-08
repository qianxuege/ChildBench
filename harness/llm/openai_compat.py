"""Client for any server that speaks the OpenAI chat-completions API.

Local Gemma examples (pick one):
    Ollama:     ollama pull gemma3:4b       -> base_url http://localhost:11434/v1, model "gemma3:4b"
    llama.cpp:  llama-server -m gemma.gguf  -> base_url http://localhost:8080/v1
    LM Studio:  start the local server      -> base_url http://localhost:1234/v1
Hosted:
    Gemini:     base_url https://generativelanguage.googleapis.com/v1beta/openai/, api_key_env GEMINI_API_KEY
    OpenAI:     base_url omitted, api_key_env OPENAI_API_KEY
"""

from __future__ import annotations

import os
from typing import Any, Optional

from . import Message, ModelError


class OpenAICompatModel:
    def __init__(self, model: str, base_url: Optional[str] = None, api_key_env: Optional[str] = None, timeout: int = 120,
                 reasoning_effort: Optional[str] = None):
        from openai import OpenAI

        key = os.environ.get(api_key_env, "") if api_key_env else ""
        if api_key_env and not key:
            raise ModelError(f"environment variable {api_key_env} is not set (put it in .env, never in the repo)")
        # Local servers ignore the key but the client requires a non-empty string.
        self.client = OpenAI(base_url=base_url, api_key=key or "not-needed", timeout=timeout)
        self.model_id = model
        self.base_url = base_url or "https://api.openai.com/v1"
        # Thinking counts against max_tokens, so a thinking model can return empty text. "none" turns it off.
        self.reasoning_effort = reasoning_effort

    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.7,
        seed: Optional[int] = None,
        max_tokens: int = 512,
        context: Optional[dict[str, Any]] = None,
    ) -> str:
        kwargs: dict[str, Any] = dict(model=self.model_id, messages=messages, temperature=temperature, max_tokens=max_tokens)
        if seed is not None:
            kwargs["seed"] = seed
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as e:  # network, auth, unsupported param
            if "seed" in kwargs and "seed" in str(e).lower():
                kwargs.pop("seed")
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise ModelError(f"{self.model_id} @ {self.base_url}: {e}") from e
        text = resp.choices[0].message.content or ""
        return text.strip()
