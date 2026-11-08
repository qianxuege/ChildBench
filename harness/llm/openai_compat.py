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
        # Running totals from each response's `usage`; run.py reads them to record tokens per conversation.
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def usage(self) -> dict[str, int]:
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}

    def preflight(self, timeout: float = 5.0) -> Optional[str]:
        """Check the server answers and serves this model. Returns an error message, or None if it's ready."""
        client = self.client.with_options(timeout=timeout, max_retries=0)
        try:
            served = {m.id for m in client.models.list()}
        except Exception as e:
            local = any(h in self.base_url for h in ("127.0.0.1", "localhost", "[::1]"))
            hint = " Is `ollama serve` running on that port?" if local else " Check the URL and the API key in .env."
            return f"can't reach {self.base_url} for model {self.model_id}: {type(e).__name__}.{hint}"
        if served and self.model_id not in served:
            return (f"{self.base_url} doesn't serve model {self.model_id} (it has: {', '.join(sorted(served)) or 'none'}). "
                    f"For Ollama: `ollama pull {self.model_id}`.")
        return None

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
        self.calls += 1
        u = getattr(resp, "usage", None)
        if u is not None:
            self.prompt_tokens += getattr(u, "prompt_tokens", 0) or 0
            self.completion_tokens += getattr(u, "completion_tokens", 0) or 0
        text = resp.choices[0].message.content or ""
        return text.strip()
