"""Manual relay: a person carries the pretend child's lines into a free chatbot app and copies the replies back
(RFC 0010, section 7, package A11).

The "model" here is a person. The orchestrator, the pretend child and its checks, the transcript format and the
judge are unchanged, so app conversations are scored like any other. Each turn:

    1. the child's line (already checked) is shown and copied to the clipboard;
    2. the person pastes it into the app (a fresh private chat) and copies the app's whole reply;
    3. the person presses Enter; the reply is read from the clipboard and shown back to confirm.

Rules for the person: paste only the lines shown here, never type your own child lines, never edit the app's
reply, copy the whole reply (refusals and warnings included), and put nothing personal into the chat.

Clipboard: macOS `pbcopy`/`pbpaste`, otherwise the optional `pyperclip` package, otherwise none (the person pastes
into the terminal instead). Config:

    agents:
      - name: chatgpt-app
        model: {backend: manual, app: ChatGPT, mode: Temporary Chat}
        # optional: clipboard: false   (always paste by hand)
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any, Callable, Optional

from . import Message, ModelError


class ManualStop(ModelError):
    """The person chose to stop; the conversation so far is saved as incomplete and not scored."""


class Clipboard:
    """System clipboard, or unavailable. `copy`/`paste` never raise; `available` says whether they work."""

    def __init__(self, enabled: bool = True):
        self._copy: Optional[Callable[[str], None]] = None
        self._paste: Optional[Callable[[], str]] = None
        if not enabled:
            return
        if shutil.which("pbcopy") and shutil.which("pbpaste"):
            self._copy = lambda text: subprocess.run(["pbcopy"], input=text, text=True, check=True)
            self._paste = lambda: subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout
            return
        try:
            import pyperclip  # optional: pip install -e ".[app]"

            self._copy, self._paste = pyperclip.copy, pyperclip.paste
        except Exception:
            pass

    @property
    def available(self) -> bool:
        return self._copy is not None and self._paste is not None

    def copy(self, text: str) -> bool:
        try:
            if self._copy:
                self._copy(text)
                return True
        except Exception:
            pass
        return False

    def paste(self) -> Optional[str]:
        try:
            return self._paste() if self._paste else None
        except Exception:
            return None


class ManualModel:
    def __init__(self, app: str, mode: str = "private chat", *, clipboard: Optional[Clipboard] = None,
                 input_fn: Optional[Callable[[str], str]] = None, print_fn: Optional[Callable[..., None]] = None):
        self.app = app
        self.mode = mode
        self.model_id = f"manual:{app}"
        self.model_shown: Optional[str] = None   # what the app says it runs, asked at the start of each conversation
        self.quit_requested = False                # set by run.py when the person presses Ctrl+C: skip the rest of the run
        self.label = ""                            # set by run.py: "<scenario> · <chatbot>"
        self.max_turns = 0                         # set by run.py, for the "turn N of M" line
        self.clipboard = clipboard if clipboard is not None else Clipboard()
        # Looked up at call time, so tests can patch builtins.input / print.
        self._in = input_fn or (lambda prompt: input(prompt))
        self._out = print_fn or (lambda *a, **k: print(*a, **k))

    # -- helpers ---------------------------------------------------------
    def _ask(self, prompt: str) -> str:
        try:
            return self._in(prompt)
        except EOFError as e:
            raise ManualStop("input ended") from e

    def _paste_by_hand(self) -> str:
        self._out("  Paste the app's whole reply, then a line with just END:")
        lines: list[str] = []
        while True:
            line = self._ask("")
            if line.strip() == "END":
                return "\n".join(lines)
            lines.append(line)

    def _read_reply(self, child_line: str) -> str:
        use_clipboard = self.clipboard.available
        while True:
            if use_clipboard:
                reply = self.clipboard.paste() or ""
                if not reply.strip():
                    self._out("  The clipboard is empty. Copy the app's reply, then press Enter (or p to paste by hand).")
                    use_clipboard = self._ask("> ").strip().lower() != "p"
                    continue
                if reply.strip() == child_line.strip():
                    self._out("  The clipboard still holds the child's line. Copy the app's reply, then press Enter.")
                    use_clipboard = self._ask("> ").strip().lower() != "p"
                    continue
            else:
                reply = self._paste_by_hand()
            words = len(reply.split())
            preview = reply if len(reply) <= 400 else reply[:400] + " …"
            self._out(f"  Got {words} words{' from the clipboard' if use_clipboard else ''}:")
            self._out("  \"" + preview.replace("\n", "\n   ") + "\"")
            choice = self._ask("  Enter = keep · r = read the clipboard again · p = paste by hand · s = stop and save\n> ")
            choice = choice.strip().lower()
            if choice == "":
                return reply
            if choice == "s":
                raise ManualStop("stopped by the person relaying")
            if choice == "p":
                use_clipboard = False
            elif choice == "r":
                use_clipboard = self.clipboard.available

    # -- ChatModel interface ------------------------------------------------
    def complete(self, messages: list[Message], *, temperature: float = 0.7, seed: Optional[int] = None,
                 max_tokens: int = 512, context: Optional[dict[str, Any]] = None) -> str:
        user_turns = [m for m in messages if m["role"] == "user"]
        if not user_turns:
            raise ModelError("manual relay: no child line to pass on")
        child_line = user_turns[-1]["content"]
        turn = len(user_turns)
        if turn == 1:
            self._out(f"\n=== New conversation: {self.label or self.app} ===")
            self._out(f"Start a new conversation in {self.app}: {self.mode}.")
            shown = self._ask(
                f"Which model name or version does {self.app} show? For example \"GPT-6.1 Sol\" or \"Claude Opus 5.5\".\n"
                f"Type it exactly as the app shows it. Press Enter if it doesn't show one.\n").strip()
            self.model_shown = shown or None
        of = f" of {self.max_turns}" if self.max_turns else ""
        self._out(f"\n{self.label or self.app} · turn {turn}{of}")
        copied = self.clipboard.copy(child_line)
        self._out(f"Child line (checked{', copied to your clipboard' if copied else ''}):")
        self._out(f"  {child_line}\n")
        self._out(f"1. Paste it into {self.app} ({self.mode}).")
        self._out(f"2. Copy {self.app}'s whole reply.")
        self._out("3. Press Enter here (or s to stop and save).")
        if self._ask("> ").strip().lower() == "s":
            raise ManualStop("stopped by the person relaying")
        return self._read_reply(child_line)
