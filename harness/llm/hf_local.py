"""Run a Hugging Face model folder in-process (no server).

Meant for the gemma-3-270m-it folder: download the whole folder from Drive
(config.json, model.safetensors, tokenizer files) and point `path` at it.
Needs the optional extras:  pip install -e ".[local]"

A 270M model is fine for smoke tests and as a weak "agent under test", but it is
too small to be a reliable judge or child simulator.
"""

from __future__ import annotations

from typing import Any, Optional

from . import Message, ModelError


class HFLocalModel:
    def __init__(self, path: str, device: str = "auto"):
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as e:
            raise ModelError('hf_local needs torch and transformers: pip install -e ".[local]"') from e
        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(path)
        self.model = AutoModelForCausalLM.from_pretrained(
            path, torch_dtype="auto", device_map=None if device == "cpu" else device
        )
        self.model_id = f"hf_local:{path.rstrip('/').split('/')[-1]}"

    @staticmethod
    def _fold_system(messages: list[Message]) -> list[Message]:
        # Some Gemma chat templates reject a separate system role: fold it into the first user turn.
        if not messages or messages[0]["role"] != "system":
            return messages
        sys_text, rest = messages[0]["content"], list(messages[1:])
        if rest and rest[0]["role"] == "user":
            rest[0] = {"role": "user", "content": f"{sys_text}\n\n{rest[0]['content']}"}
        else:
            rest.insert(0, {"role": "user", "content": sys_text})
        return rest

    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.7,
        seed: Optional[int] = None,
        max_tokens: int = 512,
        context: Optional[dict[str, Any]] = None,
    ) -> str:
        torch = self._torch
        if seed is not None:
            torch.manual_seed(seed)
        try:
            ids = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt")
        except Exception:
            ids = self.tokenizer.apply_chat_template(self._fold_system(messages), add_generation_prompt=True, return_tensors="pt")
        ids = ids.to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(
                ids,
                max_new_tokens=max_tokens,
                do_sample=temperature > 0,
                temperature=max(temperature, 1e-5),
            )
        return self.tokenizer.decode(out[0][ids.shape[-1]:], skip_special_tokens=True).strip()
