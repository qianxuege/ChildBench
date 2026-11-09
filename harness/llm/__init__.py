"""Model backends behind one small interface.

Every role (pretend-child generator, reviewer, agent under test, judge) talks to a
`ChatModel`. Swapping Gemma for Gemini or Claude is a config change, not a code
change.

Backends:
    mock           offline, deterministic; for wiring tests and demos (labels mean nothing)
    openai_compat  any OpenAI-compatible server: Ollama, llama.cpp, LM Studio, vLLM,
                   Gemini's OpenAI endpoint, OpenAI itself
    hf_local       load a Hugging Face model folder directly (e.g. gemma-3-270m-it)
    manual         a person relays lines to a free chatbot app (chatbots under test only; RFC 0010, A11)
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

Message = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": "..."}


class ChatModel(Protocol):
    model_id: str

    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.7,
        seed: Optional[int] = None,
        max_tokens: int = 512,
        context: Optional[dict[str, Any]] = None,
    ) -> str:
        """Return the assistant text. `context` is structured data that only mock
        backends read; real backends see the same information in `messages`."""
        ...


class ModelError(RuntimeError):
    pass


def make_model(spec: dict[str, Any]) -> ChatModel:
    backend = spec.get("backend", "mock")
    if backend == "mock":
        from .mock import make_mock

        return make_mock(spec)
    if backend == "openai_compat":
        from .openai_compat import OpenAICompatModel

        return OpenAICompatModel(
            model=spec["model"],
            base_url=spec.get("base_url"),
            api_key_env=spec.get("api_key_env"),
            timeout=spec.get("timeout", 120),
            reasoning_effort=spec.get("reasoning_effort"),
        )
    if backend == "hf_local":
        from .hf_local import HFLocalModel

        return HFLocalModel(path=spec["path"], device=spec.get("device", "auto"))
    if backend == "manual":
        from .manual import Clipboard, ManualModel

        return ManualModel(app=spec["app"], mode=spec.get("mode", "private chat"),
                           clipboard=Clipboard(enabled=spec.get("clipboard", True)))
    raise ValueError(f"unknown backend: {backend}")
