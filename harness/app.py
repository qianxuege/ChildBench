"""`make app`: the site plus a small API, on this computer only (RFC 0010, package F5).

    make app [PORT=11511] [CONFIGS=configs]

Lets a researcher relay chats from the site's Run page instead of the terminal, and make, send and import scoring
packets from the Score page. Python standard library only.

Rules it keeps:
    - listens on 127.0.0.1 only; refuses any other Host header, and any Origin other than its own
    - every /api/ call except /api/status needs the token printed at start (a new one each start), so another site
      open in the same browser can't drive it
    - keys stay in .env: the page never gets a key, a server address or a model setting, only names
    - it reuses the terminal's code: Run (harness/run.py), converse, the pretend child and its checks, the relay,
      the judge, make labels and make import-labels. A pasted reply is stored exactly as pasted
    - guardrails can't be turned off here: a config with guardrails.enforce false isn't offered, and only configs
      whose chatbots all use the manual relay are, so nothing paid runs from a click
    - the Score page never gets private/ (the blind key, judge scores); only send/ and a short import summary

One queue is one run folder. Reopening a queue resumes it: finished chats are skipped, unfinished ones start again,
and the unfinished attempt is kept, marked incomplete. Scoring runs once, from a button, when the queue ends.
"""

from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import re
import secrets
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import yaml
from dotenv import load_dotenv

from . import REPO_ROOT
from .labels import LabelError
from .labels import build as build_packet
from .llm.manual import ManualModel, WebRelay
from .run import PLAN_FILE, Run, load_scenarios, read_jsonl
from .validate_judge import import_labels

SITE = REPO_ROOT / "site"
MAX_BODY = 4 * 1024 * 1024
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
TOKEN_HEADER = "X-ChildBench-Token"


class AppError(Exception):
    """A request the page can fix: shown to the person as it is."""


def _safe_name(s: Any, what: str) -> str:
    if not isinstance(s, str) or not NAME.fullmatch(s) or ".." in s:
        raise AppError(f"bad {what}")
    return s


# ------------------------------------------------------------------ configs and runs


def relay_configs(config_dir: Path) -> list[dict]:
    """Configs the Run page offers: every chatbot uses the manual relay and guardrails are on."""
    out = []
    for f in sorted(config_dir.glob("*.yaml")):
        try:
            cfg = yaml.safe_load(f.read_text()) or {}
            agents = cfg.get("agents") or []
        except Exception:
            continue
        if not agents or any((a.get("model") or {}).get("backend") != "manual" for a in agents):
            continue
        if not (cfg.get("guardrails") or {}).get("enforce", True):
            continue
        try:
            _, _, personas, _ = load_scenarios(cfg, None, quiet=True)
        except SystemExit as e:
            out.append({"file": f.name, "run_name": cfg.get("run_name", f.stem), "problem": str(e)})
            continue
        out.append({
            "file": f.name, "run_name": cfg.get("run_name", f.stem), "seeds": cfg.get("seeds", [1]),
            "scenarios": [{"id": p.id, "situation": p.situation, "child_age": p.child_age, "max_turns": p.max_turns}
                          for p in personas.values()],
            "agents": [{"name": a["name"], "app": a["model"].get("app", a["name"]), "mode": a["model"].get("mode", "private chat")}
                       for a in agents],
        })
    return out


def run_progress(run_dir: Path) -> Optional[dict]:
    """Progress of a run folder started with a plan, or None for other folders."""
    plan_file = run_dir / PLAN_FILE
    if not plan_file.exists():
        return None
    try:
        plan = json.loads(plan_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    cfg = plan.get("config") or {}
    agents = [a["name"] for a in cfg.get("agents", []) if not plan.get("agents") or a["name"] in plan["agents"]]
    rows = read_jsonl(run_dir / "transcripts.jsonl")
    done = {d["transcript_id"] for d in rows if d.get("complete", True)}
    try:
        _, _, personas, _ = load_scenarios(cfg, plan.get("only"), quiet=True)
        total = len(personas) * len(agents) * len(plan.get("seeds") or [1])
    except SystemExit:
        total = None
    manual = all((a.get("model") or {}).get("backend") == "manual" for a in cfg.get("agents", []))
    return {"run_id": run_dir.name, "config_file": Path(plan.get("config_file", "")).name, "done": len(done), "total": total,
            "attempts": len(rows), "scored": (run_dir / "manifest.json").exists(), "relay": manual}


# ------------------------------------------------------------------ the queue


class Queue:
    """The one queue this server is working on: a Run, its relay, and the chat being talked, if any."""

    def __init__(self, run: Run):
        self.run = run
        self.relay = WebRelay()
        for a in run.agents:
            if isinstance(a.model, ManualModel):
                a.model.web = self.relay
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.current: Optional[str] = None       # transcript id being talked
        self.current_agent: Optional[str] = None
        self.last: Optional[dict] = None         # the chat that just ended
        self.status = "ready"                    # ready | talking | scoring | scored | error
        self.error: Optional[str] = None
        self.summary: Optional[dict] = None
        self.model_shown: dict[str, str] = {}    # last name each app showed, to prefill the box

    # -- state, as the page sees it
    def state(self) -> dict:
        r = self.run
        todo = r.todo() if self.status != "scored" else []
        pending = self.relay.pending() if self.status == "talking" else None
        if pending:
            pending["model_shown"] = self.model_shown.get(self.current_agent or "", "")
        nxt = todo[0] if todo else None
        return {
            "run_id": r.run_id, "status": self.status, "error": self.error,
            "done": len(r.plan) - len(todo), "total": len(r.plan),
            "current": self.current, "pending": pending, "last": self.last,
            "next": {"scenario": nxt[0].id, "agent": nxt[1].name, "app": nxt[1].model.app if isinstance(nxt[1].model, ManualModel) else nxt[1].name,
                     "mode": getattr(nxt[1].model, "mode", ""), "seed": nxt[2],
                     "caregiver": bool(nxt[0].caregiver_message)} if nxt else None,
            "summary": self.summary,
        }

    # -- actions
    def start_next(self) -> None:
        with self.lock:
            if self.status not in ("ready",):
                raise AppError(f"can't start a chat while the queue is {self.status}")
            todo = self.run.todo()
            if not todo:
                raise AppError("every chat in this queue is done; score it")
            p, a, s = todo[0]
            self.relay.reset()
            self.status, self.error, self.current, self.last = "talking", None, Run.tid(p, a, s), None
            self.current_agent = a.name
            self.thread = threading.Thread(target=self._talk, args=(p, a, s), daemon=True)
            self.thread.start()

    def _talk(self, p, a, s) -> None:
        try:
            t = self.run.talk(p, a, s)
            if isinstance(a.model, ManualModel) and a.model.model_shown:
                self.model_shown[a.name] = a.model.model_shown
            self.last = {"transcript_id": t.transcript_id, "complete": t.complete, "ended_because": t.ended_because,
                         "turns": [{"turn": x.turn, "speaker": x.speaker, "text": x.text} for x in t.turns]}
            self.status = "ready"
        except BaseException as e:   # a crash in a worker thread must show on the page, not vanish
            traceback.print_exc()
            self.status, self.error = "error", f"the chat stopped with an error: {type(e).__name__}. See the terminal."
        finally:
            self.current = None

    def reply(self, seq: Any, reply: Any, model_shown: Any) -> None:
        if self.status != "talking":
            raise AppError("no chat is waiting for a reply")
        if not isinstance(seq, int):
            raise AppError("bad seq")
        shown = model_shown if isinstance(model_shown, str) else None
        err = self.relay.answer(seq, reply, shown)
        if err:
            raise AppError(err)

    def stop(self) -> None:
        if self.status != "talking":
            raise AppError("no chat is running")
        self.relay.stop()
        if self.thread:
            self.thread.join(timeout=30)

    def score(self) -> None:
        with self.lock:
            if self.status != "ready":
                raise AppError(f"can't score while the queue is {self.status}")
            if self.run.todo():
                raise AppError("score when the queue ends: some chats aren't done yet")
            self.status = "scoring"
        threading.Thread(target=self._score, daemon=True).start()

    def _score(self) -> None:
        try:
            self.run.score()
            ctx = self.run.ctx
            self.summary = {"chats": len(ctx.transcripts), "scored": len(ctx.scored), "review_items": len(ctx.queued)}
            self.status = "scored"
        except BaseException as e:
            traceback.print_exc()
            self.status, self.error = "error", f"scoring stopped with an error: {type(e).__name__}. See the terminal."


# ------------------------------------------------------------------ the app


class App:
    def __init__(self, *, port: int, results: Path, configs: Path, token: Optional[str] = None,
                 skip_preflight: bool = False, rubric: Path = REPO_ROOT / "rubrics" / "v1.yaml"):
        self.port, self.results, self.configs = port, results, configs
        self.token = token or secrets.token_urlsafe(24)
        self.skip_preflight, self.rubric = skip_preflight, rubric
        self.queue: Optional[Queue] = None
        self.lock = threading.Lock()

    # -- origin rules
    def hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def origin_ok(self, headers) -> Optional[str]:
        """None when the request may go on, else why not."""
        if headers.get("Host", "") not in self.hosts():
            return "wrong host"
        origin = headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self.hosts()}:
            return "wrong origin"
        site = headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            return "cross-site request"
        return None

    def token_ok(self, headers) -> bool:
        return hmac.compare_digest(headers.get(TOKEN_HEADER, ""), self.token)

    # -- API
    def api(self, method: str, path: str, body: dict) -> Any:
        q = self.queue
        if method == "GET" and path == "/api/configs":
            return {"configs": relay_configs(self.configs)}
        if method == "GET" and path == "/api/queues":
            runs = [r for d in sorted(self.results.glob("*/"), reverse=True) if (r := run_progress(d))]
            return {"queues": [r for r in runs if r["relay"] and not r["scored"]], "active": q.run.run_id if q else None}
        if method == "POST" and path == "/api/queue/start":
            return self._start(body)
        if method == "POST" and path == "/api/queue/resume":
            return self._resume(body)
        if method == "GET" and path == "/api/state":
            return {"queue": q.state() if q else None}
        if not q and path in ("/api/next", "/api/reply", "/api/stop", "/api/score"):
            raise AppError("no queue is open; choose or resume one")
        if method == "POST" and path == "/api/next":
            q.start_next()
            return {"queue": q.state()}
        if method == "POST" and path == "/api/reply":
            q.reply(body.get("seq"), body.get("reply"), body.get("model_shown"))
            return {"ok": True}
        if method == "POST" and path == "/api/stop":
            q.stop()
            return {"queue": q.state()}
        if method == "POST" and path == "/api/score":
            q.score()
            return {"queue": q.state()}
        if method == "POST" and path == "/api/queue/close":
            with self.lock:
                if q and q.status in ("talking", "scoring"):
                    raise AppError(f"the queue is {q.status}; stop the chat first")
                self.queue = None
            return {"ok": True}
        # -- scoring (B15's packet and import)
        if method == "GET" and path == "/api/runs":
            runs = []
            for d in sorted(self.results.glob("*/"), reverse=True):
                if (d / "manifest.json").exists() and (d / "transcripts.jsonl").exists():
                    m = json.loads((d / "manifest.json").read_text())
                    runs.append({"run_id": d.name, "chats": m.get("counts", {}).get("transcripts"),
                                 "rubric": (m.get("rubric") or {}).get("version")})
            return {"runs": runs, "packets": self._packets()}
        if method == "POST" and path == "/api/packet":
            return self._make_packet(body)
        m = re.fullmatch(r"/api/packet/([^/]+)/(packet\.json|score\.html)", path)
        if method == "GET" and m:
            return self._send_file(_safe_name(m.group(1), "packet name"), m.group(2))
        m = re.fullmatch(r"/api/packet/([^/]+)/returned", path)
        if method == "POST" and m:
            return self._returned(_safe_name(m.group(1), "packet name"), body)
        raise AppError("unknown request")

    def _open(self, run: Run) -> dict:
        with self.lock:
            if self.queue and self.queue.status in ("talking", "scoring"):
                raise AppError(f"another queue is {self.queue.status}; stop it first")
            self.queue = Queue(run)
            return {"queue": self.queue.state()}

    def _start(self, body: dict) -> dict:
        file = _safe_name(body.get("config"), "config")
        if file not in {c["file"] for c in relay_configs(self.configs) if "problem" not in c}:
            raise AppError("that config can't be run here: its chatbots must all use the relay, with guardrails on")
        cfg_path = self.configs / file
        cfg = yaml.safe_load(cfg_path.read_text())
        only = body.get("only") or None
        agents = body.get("agents") or None
        seeds = body.get("seeds") or None
        for v, what in ((only, "scenarios"), (agents, "apps")):
            if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise AppError(f"bad {what}")
        if seeds is not None and not (isinstance(seeds, list) and seeds and all(isinstance(x, int) and 0 <= x < 10**6 for x in seeds)):
            raise AppError("seeds must be a list of whole numbers")
        try:
            rubric, rubric_path, personas, hashes = load_scenarios(cfg, only, quiet=True)
            shown = cfg_path.relative_to(REPO_ROOT) if cfg_path.is_relative_to(REPO_ROOT) else cfg_path
            run = Run(cfg, str(shown), rubric, rubric_path, personas, hashes, seeds or cfg.get("seeds", [1]),
                      out_root=self.results, only=only, agent_names=agents, skip_preflight=self.skip_preflight)
        except SystemExit as e:
            raise AppError(str(e)) from None
        print(f"queue {run.run_id}: {len(run.plan)} chats")
        return self._open(run)

    def _resume(self, body: dict) -> dict:
        run_id = _safe_name(body.get("run_id"), "run id")
        if self.queue and self.queue.run.run_id == run_id:
            return {"queue": self.queue.state()}
        d = self.results / run_id
        prog = run_progress(d)
        if not prog or not prog["relay"]:
            raise AppError("that run can't be resumed here")
        try:
            run = Run.reopen(d, skip_preflight=self.skip_preflight)
        except SystemExit as e:
            raise AppError(str(e)) from None
        print(f"queue {run.run_id} resumed: {len(run.plan) - len(run.todo())} of {len(run.plan)} done")
        return self._open(run)

    # -- scoring packets: only send/ goes to the page; returned/ takes uploads; private/ is never read here
    def _labeling(self) -> Path:
        return self.results / "labeling"

    def _packets(self) -> list[dict]:
        out = []
        for d in sorted(self._labeling().glob("*/"), reverse=True):
            if (d / "send" / "packet.json").exists():
                returned = sorted(p.name for p in (d / "returned").glob("*.csv")) if (d / "returned").is_dir() else []
                out.append({"name": d.name, "page": (d / "send" / "score.html").exists(), "returned": returned,
                            "imported": (d / "private" / "agreement.json").exists()})
        return out

    def _make_packet(self, body: dict) -> dict:
        run_id = _safe_name(body.get("run_id"), "run id")
        folder = self.results / run_id
        if not (folder / "manifest.json").exists():
            raise AppError("that run isn't scored yet; finish its queue and press Score first")
        name = f"{run_id}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        try:
            r = build_packet(folder, self.rubric, self._labeling() / name)
        except LabelError as e:
            raise AppError(str(e)) from None
        packet = json.loads((self._labeling() / name / "send" / "packet.json").read_text(encoding="utf-8"))
        warnings = []
        if r["names_itself"]:
            warnings.append(f"a reply names an AI app or company, so these chats may not be blind: {', '.join(r['names_itself'])}")
        if r["skipped"]:
            warnings.append(f"{len(r['skipped'])} unfinished chat(s) left out")
        return {"name": name, "chats": r["chats"], "page": r["page"], "warnings": warnings, "packet": packet}

    def _send_file(self, name: str, file: str) -> tuple[bytes, str, str]:
        p = self._labeling() / name / "send" / file
        if not p.is_file():
            raise AppError(f"no {file} in that packet")
        return p.read_bytes(), mimetypes.guess_type(file)[0] or "application/octet-stream", f"{name}-{file}"

    def _returned(self, name: str, body: dict) -> dict:
        folder = self._labeling() / name
        if not (folder / "send" / "packet.json").exists():
            raise AppError("no such packet")
        file, text = body.get("filename"), body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise AppError("the CSV is empty")
        file = _safe_name(file, "file name")
        if not file.lower().endswith(".csv"):
            raise AppError("upload the CSV the scoring page downloaded")
        (folder / "returned").mkdir(exist_ok=True)
        (folder / "returned" / file).write_text(text, encoding="utf-8")
        try:
            rep = import_labels(folder)
        except LabelError as e:   # e.g. still waiting for the other scorer: the file is kept, nothing is imported
            return {"saved": file, "imported": False, "message": str(e)}
        hh = rep["comparisons"].get("A_vs_B", {})
        return {"saved": file, "imported": True,
                "message": f"imported {rep['chats']} chats; {rep['disagreements']} answers differ between the scorers. "
                           f"Agreement and the scores are in {folder.relative_to(self.results.parent) if folder.is_relative_to(self.results.parent) else folder}/private/ "
                           "on this computer; this page doesn't show them.",
                "has_agreement": bool(hh)}


# ------------------------------------------------------------------ HTTP


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "childbench-app"

        def log_request(self, code="-", size="-"):   # quiet unless refused or failed; chat text is never in the URL
            try:
                if int(code) < 400:
                    return
            except (TypeError, ValueError):
                pass
            super().log_request(code, size)

        def _send(self, code: int, data: bytes, ctype: str, extra: Optional[dict] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _handle(self, method: str) -> None:
            why = app.origin_ok(self.headers)
            if why:
                return self._json(403, {"ok": False, "error": f"refused: {why}"})
            path = unquote(urlparse(self.path).path)
            if path == "/api/status":
                return self._json(200, {"app": True, "port": app.port})
            if path.startswith("/api/"):
                if not app.token_ok(self.headers):
                    return self._json(403, {"ok": False, "error": "refused: missing or wrong token; open the link `make app` printed"})
                body: dict = {}
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        return self._json(413, {"ok": False, "error": "too large"})
                    if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                        return self._json(415, {"ok": False, "error": "send JSON"})
                    try:
                        body = json.loads(self.rfile.read(n) or b"{}")
                    except json.JSONDecodeError:
                        return self._json(400, {"ok": False, "error": "bad JSON"})
                    if not isinstance(body, dict):
                        return self._json(400, {"ok": False, "error": "bad JSON"})
                try:
                    out = app.api(method, path, body)
                except AppError as e:
                    return self._json(400, {"ok": False, "error": str(e)})
                except Exception as e:
                    traceback.print_exc()
                    return self._json(500, {"ok": False, "error": f"server error: {type(e).__name__}; see the terminal"})
                if isinstance(out, tuple):
                    data, ctype, filename = out
                    return self._send(200, data, ctype, {"Content-Disposition": f'attachment; filename="{filename}"'})
                return self._json(200, {"ok": True, **out})
            if method != "GET":
                return self._json(405, {"ok": False, "error": "not allowed"})
            return self._static(path)

        def _static(self, path: str) -> None:
            rel = path.lstrip("/") or "index.html"
            target = (SITE / rel).resolve()
            if not target.is_relative_to(SITE.resolve()) or not target.is_file():
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            data = target.read_bytes()
            if rel == "score.html":
                # The Score page's make app controls live in run.js and are added only here, so site/score.html, the
                # public site and the send/score.html built from it never hold code that calls out (F4's rule).
                data = data.replace(b"</head>", b'<link rel="stylesheet" href="assets/run.css">\n</head>', 1)
                data = data.replace(b"</body>", b'<script src="assets/run.js" defer></script>\n</body>', 1)
            self._send(200, data, ctype)

        def do_GET(self):
            self._handle("GET")

        def do_HEAD(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


def serve(app: App) -> ThreadingHTTPServer:
    """A started server for `app` (in a background thread). Tests use port 0 and read the real port back."""
    httpd = ThreadingHTTPServer(("127.0.0.1", app.port), make_handler(app))
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The site plus the Run page, on this computer only.")
    ap.add_argument("--port", type=int, default=11511)
    ap.add_argument("--results", default="results")
    ap.add_argument("--configs", default="configs", help="folder of configs to offer (only relay configs are shown)")
    ap.add_argument("--skip-preflight", action="store_true", help="don't check that model servers are up before a queue starts")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")
    app = App(port=a.port, results=REPO_ROOT / a.results, configs=REPO_ROOT / a.configs, skip_preflight=a.skip_preflight)
    try:
        httpd = serve(app)
    except OSError as e:
        print(f"can't listen on 127.0.0.1:{a.port}: {e}. Try make app PORT=<another port>.", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{app.port}/run.html#token={app.token}"
    print(f"ChildBench app on this computer only. Open:\n  {url}\nThe link holds this session's token; it changes each start. Ctrl+C to stop.", flush=True)
    if not a.no_browser:
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopped. An unfinished chat is saved as incomplete only if you pressed Stop; otherwise it starts again on resume.")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
