"""The chatbot being graded. Its replies are NEVER edited or filtered (RFC 0006):
otherwise we would be grading our filter, not the chatbot."""

from __future__ import annotations

from typing import Optional

from ..llm import ChatModel
from ..schemas import Turn


class AgentUnderTest:
    def __init__(self, name: str, model: ChatModel, *, system_prompt: Optional[str] = None,
                 temperature: float = 0.7, max_tokens: int = 400, mock_behavior: Optional[str] = None):
        self.name = name
        self.model = model
        self.system_prompt = system_prompt   # None = test the model as shipped, no extra instructions
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.mock_behavior = mock_behavior

    @property
    def model_id(self) -> str:
        return self.model.model_id

    def reply(self, history: list[Turn], seed: int) -> str:
        msgs = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        for t in history:
            msgs.append({"role": "user" if t.speaker == "child" else "assistant", "content": t.text})
        text = self.model.complete(msgs, temperature=self.temperature, seed=seed, max_tokens=self.max_tokens,
                                   context={"role": "chatbot", "history": history, "behavior": self.mock_behavior})
        return text.strip() or "(no reply)"
