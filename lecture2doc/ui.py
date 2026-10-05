"""A small local web UI: pick the folders, pick the lectures, watch the progress, edit the glossary.

Standard library only. The server listens on 127.0.0.1, so it is only reachable from this computer, and it
rejects requests that do not come from its own page."""
from __future__ import annotations

import difflib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lecture2doc import pipeline
from lecture2doc.config import ConfigError, check_ffmpeg, load_config

log = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
PORTS = range(8765, 8785)
STATIC = Path(__file__).parent / "ui_static"
SETTINGS_FILE = Path.home() / ".lecture2doc" / "settings.json"
TASKS = ("glossary", "cleanup", "transcribe")   # the tasks the user chooses AI models for
MODELS_CACHE_SECONDS = 300
OPTION_KEYS = {"cleanup": bool, "keep_raw": bool, "thumbnails": bool, "timestamps": bool, "page_breaks": bool,
               "group_by_slide": bool, "language": str}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def pair_pdf(video_stem: str, pdf_names: list[str]) -> str | None:
    """The PDF whose name is the same as the video's (ignoring case and punctuation), else one whose name continues
    it, else the most similar one."""
    if not pdf_names:
        return None
    key = _norm(video_stem)
    stems = {name: _norm(Path(name).stem) for name in pdf_names}
    for name, stem in stems.items():
        if stem == key:
            return name
    # one name continues the other ("lecture-1" and "lecture-1-architectures"): take the closest in length
    prefixed = [n for n, stem in stems.items() if stem and (stem.startswith(key) or key.startswith(stem))]
    if prefixed:
        return min(prefixed, key=lambda n: abs(len(stems[n]) - len(key)))
    best = max(pdf_names, key=lambda n: difflib.SequenceMatcher(None, key, stems[n]).ratio())
    return best if difflib.SequenceMatcher(None, key, stems[best]).ratio() >= 0.6 else None


def natural_key(name: str) -> list:
    """Sort key for file names: case-insensitive, and numbers by value (lecture 2 before lecture 10)."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.lower())]


def list_files(folder: str, suffixes: set[str]) -> list[str]:
    try:
        return sorted((p.name for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in suffixes),
                      key=natural_key)
    except OSError:
        return []


class UiReporter(pipeline.Reporter):
    """Writes the progress of one lecture into its state dict (read by the page every second)."""

    def __init__(self, state: dict, lock: threading.Lock, cancel: threading.Event):
        super().__init__()
        self.state, self.lock, self.cancel = state, lock, cancel

    def check(self):
        if self.cancel.is_set():
            raise pipeline.Cancelled()

    def emit(self, key, label, overall, fraction, detail):
        with self.lock:
            self.state.update(stage=key, label=label, overall=overall, fraction=fraction, detail=detail)

    def message(self, text):
        with self.lock:
            self.state["log"] = (self.state["log"] + [text])[-300:]


class App:
    def __init__(self, settings_file: Path = SETTINGS_FILE):
        self.settings_file = settings_file
        self.lock = threading.RLock()
        self.glossary_job = {"building": False, "error": None, "phase": "",
                             "progress": {"decks_done": 0, "decks_total": 0, "parts_done": 0, "parts_total": 0}}
        self.settings = {"pdf_dir": "", "video_dir": "", "out_dir": str(Path.cwd() / "output"),
                         "options": {"cleanup": True, "keep_raw": False, "thumbnails": True, "timestamps": True,
                                     "page_breaks": True, "group_by_slide": False, "language": ""},
                         "pairs": {}, "models": {task: [] for task in TASKS}}  # [] = the default of the task
        self._models_cache = (0.0, [])
        try:
            saved = json.loads(settings_file.read_text(encoding="utf-8"))
            self.settings.update({k: v for k, v in saved.items() if k in self.settings})
        except (OSError, ValueError):
            pass
        self.jobs: dict[str, dict] = {}      # by video file name
        self.cancels: dict[str, threading.Event] = {}
        self.queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    # -- settings ---------------------------------------------------------------------------------------------
    def save_settings(self) -> None:
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings_file.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("cannot save the settings: %s", exc)

    # -- what is on disk ----------------------------------------------------------------------------------------
    def workdir(self, video: str) -> Path:
        return Path(self.settings["out_dir"]) / ".lecture2doc" / Path(video).stem

    def output(self, video: str) -> Path:
        return Path(self.settings["out_dir"]) / f"{Path(video).stem}.docx"

    def problem(self) -> str | None:
        """Why nothing can be processed yet, in words for the user; None when all is set up."""
        try:
            check_ffmpeg()
            load_config()
        except ConfigError as exc:
            return str(exc)
        return None

    def lectures(self) -> list[dict]:
        pdfs = list_files(self.settings["pdf_dir"], {".pdf"})
        rows = []
        for video in list_files(self.settings["video_dir"], VIDEO_SUFFIXES):
            chosen = self.settings["pairs"].get(video)
            pdf = chosen if chosen in pdfs else pair_pdf(Path(video).stem, pdfs)
            work = self.workdir(video)
            with self.lock:
                job = dict(self.jobs.get(video) or {"status": "idle"})
                if job.get("status") == "running":
                    job["elapsed"] = round(time.time() - job["started"])
                job.pop("started", None)
            rows.append({"video": video, "pdf": pdf, "job": job,
                         "has_segments": (work / "segments.json").is_file(),
                         "docx": self.output(video).is_file()})
        return rows

    def glossary_state(self) -> dict:
        text = pipeline.read_glossary(self.settings["pdf_dir"]) if self.settings["pdf_dir"] else None
        with self.lock:
            job = dict(self.glossary_job)
        return {"exists": text is not None, "terms": len([ln for ln in (text or "").splitlines() if ln.strip()]),
                "building": job["building"], "error": job["error"], "progress": job["progress"],
                "phase": job.get("phase", "")}

    def state(self) -> dict:
        return {"settings": self.settings, "pdfs": list_files(self.settings["pdf_dir"], {".pdf"}),
                "lectures": self.lectures(), "problem": self.problem(), "glossary": self.glossary_state(),
                "default_models": self.default_models()}

    # -- actions --------------------------------------------------------------------------------------------------
    def set_folders(self, body: dict) -> None:
        for key in ("pdf_dir", "video_dir", "out_dir"):
            if isinstance(body.get(key), str):
                self.settings[key] = body[key].strip().strip('"')
        self.save_settings()

    def default_models(self) -> dict[str, list[str]]:
        """What each task uses while the user has not chosen: the .env settings, else the default models."""
        try:
            cfg = load_config()
            return {task: list(cfg.default_models(task)) for task in TASKS}
        except ConfigError:
            from lecture2doc.config import DEFAULT_CHAT_MODELS

            return {"glossary": list(DEFAULT_CHAT_MODELS), "cleanup": list(DEFAULT_CHAT_MODELS),
                    "transcribe": ["alias-stt"]}

    def api_models(self) -> list[dict]:
        """Every model the API offers (cached for a few minutes): id, kind (chat / audio_transcription / ...),
        and context size."""
        from lecture2doc.api import make_client

        stamp, cached = self._models_cache
        if cached and time.time() - stamp < MODELS_CACHE_SECONDS:
            return cached
        try:
            data = make_client(load_config()).models.list().data
        except ConfigError as exc:
            raise pipeline.PipelineError(str(exc)) from None
        except Exception as exc:
            raise pipeline.PipelineError(f"cannot read the list of models from the API: {exc}") from None
        models = [{"id": m.id, "mode": getattr(m, "mode", "chat"),
                   "context": getattr(m, "max_input_tokens", None)}
                  for m in data if getattr(m, "mode", "chat") in ("chat", "audio_transcription")]
        self._models_cache = (time.time(), models)
        return models

    def set_models(self, task: str, models: list) -> None:
        if task not in TASKS:
            raise pipeline.PipelineError(f"unknown task {task}")
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
            raise pipeline.PipelineError("models must be a list of names")
        self.settings["models"][task] = list(dict.fromkeys(models))
        self.save_settings()

    def set_options(self, body: dict) -> None:
        for key, kind in OPTION_KEYS.items():
            if isinstance(body.get(key), kind):
                self.settings["options"][key] = body[key].strip() if kind is str else body[key]
        self.save_settings()

    def set_pair(self, video: str, pdf: str) -> None:
        self.settings["pairs"][video] = pdf
        self.save_settings()

    def _pdf_path(self, pdf: str) -> Path:
        return Path(self.settings["pdf_dir"]) / pdf

    def _row(self, video: str) -> dict:
        for row in self.lectures():
            if row["video"] == video:
                return row
        raise KeyError(f"unknown video {video}")

    def _job(self, row: dict) -> pipeline.Job:
        opts = {**self.settings["options"]}
        language = opts.pop("language", "") or None
        video = Path(self.settings["video_dir"]) / row["video"]
        pdf = Path(self.settings["pdf_dir"]) / row["pdf"]
        return pipeline.Job(video, pdf, self.workdir(row["video"]), self.output(row["video"]),
                            pipeline.Options(language=language, models=dict(self.settings["models"]), **opts))

    def enqueue(self, videos: list[str], rebuild: bool = False) -> None:
        if (not rebuild and self.settings["options"]["cleanup"]
                and pipeline.read_glossary(self.settings["pdf_dir"]) is None):
            raise pipeline.PipelineError("Build the glossary first (section 2): it makes the transcripts much better.")
        for name in videos:
            row = self._row(name)
            with self.lock:
                if self.jobs.get(name, {}).get("status") in ("queued", "running"):
                    continue
                state = {"status": "queued", "stage": "", "label": "Waiting", "overall": 0.0, "fraction": None,
                         "detail": "", "log": [], "error": None, "started": time.time()}
                self.jobs[name] = state
                self.cancels[name] = threading.Event()
            if not row["pdf"]:
                self._fail(state, "No slide PDF is chosen for this video.")
                continue
            self.queue.put((name, row, rebuild))

    def cancel(self, video: str | None) -> None:
        with self.lock:
            for name, event in self.cancels.items():
                if video in (None, name) and self.jobs.get(name, {}).get("status") in ("queued", "running"):
                    event.set()

    def _fail(self, state: dict, message: str) -> None:
        with self.lock:
            state.update(status="error", error=message, label="Failed")

    def _worker(self) -> None:
        while True:
            name, row, rebuild = self.queue.get()
            state, cancel = self.jobs[name], self.cancels[name]
            if cancel.is_set():
                with self.lock:
                    state.update(status="cancelled", label="Stopped")
                continue
            with self.lock:
                state.update(status="running", label="Starting", started=time.time())
            reporter = UiReporter(state, self.lock, cancel)
            try:
                job = self._job(row)
                (pipeline.rebuild if rebuild else pipeline.run)(job, reporter)
                with self.lock:
                    state.update(status="done", label="Done", overall=1.0, fraction=1.0, detail="")
            except pipeline.Cancelled:
                with self.lock:
                    state.update(status="cancelled", label="Stopped", detail="")
            except pipeline.PipelineError as exc:
                self._fail(state, str(exc))
            except Exception as exc:  # shown to the user instead of a dead page
                log.exception("lecture %s failed", name)
                self._fail(state, f"{type(exc).__name__}: {exc}")
            finally:
                with self.lock:
                    state["elapsed_total"] = round(time.time() - state["started"])

    def build_glossary(self) -> None:
        """Start building the glossary of the slides folder in the background (the page polls the progress)."""
        folder = self.settings["pdf_dir"]
        if not folder or not Path(folder).is_dir():
            raise pipeline.PipelineError("Choose the slides folder first.")
        with self.lock:
            if self.glossary_job["building"]:
                return
            self.glossary_job = {"building": True, "error": None, "phase": "",
                                 "progress": {"decks_done": 0, "decks_total": 0, "parts_done": 0, "parts_total": 0}}

        def progress(decks_done: int, decks_total: int, parts_done: int, parts_total: int) -> None:
            with self.lock:
                self.glossary_job["progress"] = {"decks_done": decks_done, "decks_total": decks_total,
                                                 "parts_done": parts_done, "parts_total": parts_total}

        def phase(text: str) -> None:
            with self.lock:
                self.glossary_job["phase"] = text

        def work() -> None:
            error = None
            try:
                pipeline.make_glossary(folder, on_progress=progress, models=self.settings["models"]["glossary"] or None,
                                       say=phase)
            except pipeline.PipelineError as exc:
                error = str(exc)
            except Exception as exc:
                log.exception("glossary build failed")
                error = f"{type(exc).__name__}: {exc}"
            with self.lock:
                self.glossary_job["building"] = False
                self.glossary_job["error"] = error

        threading.Thread(target=work, daemon=True).start()

    def glossary_text(self) -> str:
        text = pipeline.read_glossary(self.settings["pdf_dir"])
        if text is None:
            raise pipeline.PipelineError("The glossary is not built yet.")
        return text

    def save_glossary(self, text: str) -> None:
        if not Path(self.settings["pdf_dir"]).is_dir():
            raise pipeline.PipelineError("Choose the slides folder first.")
        pipeline.save_glossary(self.settings["pdf_dir"], text)

    def reset(self, video: str) -> None:
        self._row(video)
        if self.jobs.get(video, {}).get("status") in ("queued", "running"):
            raise pipeline.PipelineError("Stop the run first.")
        shutil.rmtree(self.workdir(video), ignore_errors=True)
        self.jobs.pop(video, None)

    def open_path(self, kind: str, video: str | None) -> None:
        target = Path(self.settings["out_dir"])
        if kind == "file" and video:
            target = self.output(video)
        if not target.exists():
            raise pipeline.PipelineError(f"{target} does not exist yet.")
        if sys.platform.startswith("win"):
            os.startfile(target)  # noqa: S606 - a path we built ourselves
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(target)])


def pick_folder(initial: str) -> str | None:
    """Native folder dialog, in its own process (Tk wants the main thread). None if cancelled or unavailable."""
    code = ("import sys, tkinter as tk\nfrom tkinter import filedialog\nr = tk.Tk(); r.withdraw()\n"
            "r.attributes('-topmost', True)\nprint(filedialog.askdirectory(initialdir=sys.argv[1] or None, "
            "mustexist=True) or '')")
    start = initial if initial and Path(initial).is_dir() else str(Path.home())
    try:
        out = subprocess.run([sys.executable, "-c", code, start], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "lecture2doc"

    def log_message(self, *args):  # keep the terminal quiet
        pass

    def _allowed(self) -> bool:
        """Only our own page may talk to us (blocks other websites from driving a local server)."""
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _send(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode("utf-8"))

    def do_GET(self):
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        path = self.path.partition("?")[0]
        if path == "/":
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html")
        if path == "/api/state":
            return self._json(self.app.state())
        if path == "/api/models":
            return self._guarded(lambda: {"models": self.app.api_models()})
        if path == "/api/glossary":
            return self._guarded(lambda: {"text": self.app.glossary_text()})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        a, p = self.app, self.path
        routes = {
            "/api/folders": lambda: a.set_folders(body),
            "/api/options": lambda: a.set_options(body),
            "/api/models/select": lambda: a.set_models(body["task"], body["models"]),
            "/api/pair": lambda: a.set_pair(body["video"], body["pdf"]),
            "/api/pick": lambda: {"path": pick_folder(a.settings.get(body.get("kind", ""), ""))},
            "/api/run": lambda: a.enqueue(body["videos"], bool(body.get("rebuild"))),
            "/api/cancel": lambda: a.cancel(body.get("video")),
            "/api/glossary/build": lambda: a.build_glossary(),
            "/api/glossary/save": lambda: a.save_glossary(body["text"]),
            "/api/reset": lambda: a.reset(body["video"]),
            "/api/open": lambda: a.open_path(body.get("kind", "folder"), body.get("video")),
        }
        if p not in routes:
            return self._json({"error": "not found"}, 404)
        self._guarded(routes[p])

    def _guarded(self, action) -> None:
        try:
            result = action()
        except pipeline.PipelineError as exc:
            return self._json({"error": str(exc)}, 400)
        except (KeyError, TypeError):
            return self._json({"error": "bad request"}, 400)
        except Exception as exc:
            log.exception("request failed")
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        self._json(result if isinstance(result, dict) else {"ok": True})


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows this would let us share a port that another program already uses


def make_server(app: App | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app or App()})
    for port in PORTS:
        try:
            return LocalServer(("127.0.0.1", port), handler)
        except OSError:
            continue
    raise SystemExit(f"no free port between {PORTS[0]} and {PORTS[-1]}")


def serve(open_browser: bool = True) -> int:
    server = make_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"lecture2doc is running at {url}\nPress Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    return 0
