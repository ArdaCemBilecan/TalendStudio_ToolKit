"""Local web UI server (``tmt ui``). Standard library only.

Read-only: it parses job files and answers questions about them. It never writes files and never starts a process.
Security model, because it can list folders and read job files for whoever can talk to it:

* it binds to **127.0.0.1 only**, on a random port;
* every request needs a random **token** (printed in the URL), compared in constant time;
* the ``Host`` header must be the loopback address (blocks DNS rebinding) and, if present, ``Origin`` must match;
* the page is served with a strict Content-Security-Policy and ``nosniff``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import sys
import threading
import webbrowser
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from tmt import __version__
from tmt.config import load_config
from tmt.errors import TmtError
from tmt.lineage.explore import explore, masters
from tmt.lineage.svg import to_svg
from tmt.scan import ScanResult, scan

log = logging.getLogger(__name__)
MAX_BODY = 64 * 1024
MAX_TEXT = 300
CSP_PAGE = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'")


class InvalidRequest(ValueError):
    """A value that is not valid; the message says what to change."""


def _text(value: Any, what: str, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InvalidRequest(f"{what} must be text")
    value = value.strip()
    if required and not value:
        raise InvalidRequest(f"{what} is required")
    if len(value) > MAX_TEXT or any(ch in value for ch in ("\x00", "\n", "\r")):
        raise InvalidRequest(f"{what} is not valid")
    return str(value)


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, address reuse lets a second server bind a port that is already in use (and then receive nobody's or
    # somebody else's requests). Another user on the same machine must be refused so that a free port is chosen.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE"), 1)  # noqa: B009
        super().server_bind()

    def __init__(self, port: int, token: str, workspace: str | None = None) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.token = token
        self.workspace = workspace  # given on the command line: the page opens it straight away
        self.scan_cache: OrderedDict[tuple[str, str], tuple[tuple[int, float], ScanResult]] = OrderedDict()

    @property
    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}


DEFAULT_PORT = 8765  # stable, so the browser remembers the last workspace; another port is used if it is busy


def make_server(port: int = 0, token: str | None = None, workspace: str | None = None) -> UiServer:
    return UiServer(port, token or secrets.token_urlsafe(24), workspace)


# ------------------------------------------------------------------ helpers (also unit-tested)
def list_roots() -> list[str]:
    if sys.platform == "win32":
        listdrives = getattr(os, "listdrives", None)
        if listdrives:
            return [d for d in listdrives()]
        return [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{c}:\\").exists()]
    return ["/"]


def guess_workspace(home: Path | None = None) -> str | None:
    """The Talend workspace of the current user, if it can be found: $TMT_WORKSPACE, else a folder with a Talend
    project in the places Studio and people usually put it. Only folder names are looked at."""
    env = os.environ.get("TMT_WORKSPACE", "").strip()
    if env and Path(env).is_dir():
        return env
    home = home or Path.home()
    roots = [home, home / "Desktop", home / "Documents"]
    names = ("workspace", "Talend_WS", "talend_ws", "TalendWorkspace")

    def has_project(folder: Path) -> bool:
        try:
            subs = [c for c in folder.iterdir() if c.is_dir()]
            return any((c / "talend.project").is_file() or (c / "process").is_dir() for c in subs)
        except OSError:
            return False

    for root in roots:
        for name in names:
            if has_project(root / name):
                return str(root / name)
        try:
            for child in sorted(root.iterdir()):
                looks = "talend" in child.name.lower() or "workspace" in child.name.lower()
                if child.is_dir() and looks and has_project(child):
                    return str(child)
        except OSError:
            continue
    return None


def example_path() -> str | None:
    """The included demo project (a master job that starts three jobs), else the plain samples, else nothing."""
    for rel in (("samples", "talend_demo"), ("samples", "talend")):
        p = Path.cwd().joinpath(*rel)
        if p.is_dir():
            return str(p)
    return None


def browse(path_str: str) -> dict[str, Any]:
    shortcuts = [{"name": "Home", "path": str(Path.home())}]
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        shortcuts.append({"name": "Desktop", "path": str(desktop)})
    shortcuts.append({"name": "Current folder", "path": str(Path.cwd())})
    example = example_path()
    if example:
        shortcuts.append({"name": "Demo project", "path": example})
    if not path_str:
        return {"path": "", "parent": "", "entries": [{"name": r, "path": r, "project": False} for r in list_roots()],
                "files": 0, "truncated": False, "shortcuts": shortcuts}
    p = Path(path_str).expanduser().resolve()
    if not p.is_dir():
        raise InvalidRequest(f"{path_str} is not a folder")
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        children = sorted(p.iterdir(), key=lambda c: c.name.lower())
    except OSError as exc:
        raise InvalidRequest(f"cannot read {p}: {exc.strerror}") from exc
    for child in children:
        if child.name.startswith((".", "$")):
            continue
        try:
            if not child.is_dir():
                continue
            is_project = (child / "talend.project").exists() or (child / "process").is_dir()
        except OSError:
            continue
        if len(entries) >= 500:
            truncated = True
            break
        entries.append({"name": child.name, "path": str(child), "project": is_project})
    try:
        files = sum(1 for c in p.iterdir() if c.suffix == ".item")
    except OSError:
        files = 0
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "parent": parent, "entries": entries, "files": files, "truncated": truncated,
            "shortcuts": shortcuts}


def _signature(path: Path) -> tuple[int, float]:
    """Cheap fingerprint of a folder's job files: how many, and the newest modification time."""
    if path.is_file():
        return (1, path.stat().st_mtime)
    count, newest = 0, 0.0
    for p in path.rglob("*"):
        if p.suffix in (".item", ".properties", ".project") or p.name == "talend.project":
            try:
                newest = max(newest, p.stat().st_mtime)
                count += 1
            except OSError:
                continue
    return (count, newest)


def scan_cached(server: UiServer, path_str: str, context: str) -> ScanResult:
    """Scan once and reuse the result until a job file changes (picking another master must feel instant)."""
    path = _text(path_str, "Folder or job file", required=True)
    if not Path(path).exists():
        raise InvalidRequest(f"{path} does not exist")
    key = (str(Path(path).resolve()), context)
    sig = _signature(Path(path))
    hit = server.scan_cache.get(key)
    if hit is not None and hit[0] == sig:
        server.scan_cache.move_to_end(key)
        return hit[1]
    try:
        result = scan(Path(path), context=context or None, config=load_config(None))
    except TmtError as exc:
        raise InvalidRequest(str(exc)) from exc
    server.scan_cache[key] = (sig, result)
    while len(server.scan_cache) > 4:
        server.scan_cache.popitem(last=False)
    return result


MAX_WHOLE_GRAPH = 120  # drawing hundreds of jobs at once is unreadable; ask for a master job instead


def explore_api(server: UiServer, path_str: str, master: str, related: bool, context: str) -> dict[str, Any]:
    """Everything the explorer page shows for one master job (or the whole workspace)."""
    result = scan_cached(server, path_str, context)
    jobs = result.jobs
    base: dict[str, Any] = {
        "found": len(jobs), "failures": [{"path": f.path, "error": f.error} for f in result.failures],
        "masters": masters(jobs), "tools": sorted({j.source_tool for j in jobs}),
        "projects": sorted({j.project for j in jobs if j.project}),
    }
    if not jobs:
        return {**base, "summary": None, "details": {}, "svg": None, "message": "No job files found in this folder."}
    key: str | None = None
    if master:
        keys = [j.key for j in jobs]
        named = [j.key for j in jobs if j.name == master]
        if master in keys:
            key = master
        elif len(named) == 1:
            key = named[0]
        elif named:
            options = ", ".join(sorted(named))
            raise InvalidRequest(f"{master!r} exists in several projects; choose one of: {options}")
        else:
            raise InvalidRequest(f"job {master!r} not found")
    if key is None and len(jobs) > MAX_WHOLE_GRAPH:
        whole = explore(jobs, None)
        return {**base, "summary": whole.summary, "details": {}, "svg": None,
                "message": f"This workspace has {len(jobs)} jobs: too many to draw at once. Choose a master job."}
    ex = explore(jobs, key, related=related)
    svg = to_svg(ex.graph, interactive=True, highlight=f"job:{key}" if key else None)
    return {**base, "summary": ex.summary, "details": ex.details, "svg": svg, "master": key, "message": ""}


def inspect(server: UiServer, path_str: str, context: str) -> dict[str, Any]:
    """Parse the folder (same code as ``tmt scan``) and say what is in it, for the job drop-downs."""
    result = scan_cached(server, path_str, context)
    return {
        "found": len(result.jobs), "masters": masters(result.jobs),
        "jobs": [{"key": j.key, "name": j.name, "project": j.project,
                  "level": j.complexity.level if j.complexity else None,
                  "score": j.complexity.score if j.complexity else None, "flags": len(j.issues)}
                 for j in result.jobs[:2000]],
        "failures": [{"path": f.path, "error": f.error} for f in result.failures],
        "tools": sorted({j.source_tool for j in result.jobs}),
    }


# ------------------------------------------------------------------ HTTP
class _Handler(BaseHTTPRequestHandler):
    server: UiServer
    server_version = "tmt-ui"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("%s - %s", self.address_string(), format % args)

    # ---- plumbing
    def _send(self, body: bytes, ctype: str, status: int = 200, csp: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if csp:
            self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: object, status: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self.server.allowed_hosts}:
            self._error(HTTPStatus.FORBIDDEN, "unexpected Origin")
            return False
        given = self.headers.get("X-TMT-Token") or (query.get("token") or [""])[0]
        if not secrets.compare_digest(given.encode(), self.server.token.encode()):
            self._error(HTTPStatus.FORBIDDEN, "missing or wrong token: open the URL printed by `tmt ui`")
            return False
        return True

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise InvalidRequest("request too large")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise InvalidRequest("expected JSON")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as exc:
            raise InvalidRequest("invalid JSON") from exc
        if not isinstance(data, dict):
            raise InvalidRequest("expected a JSON object")
        return data

    # ---- GET
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/favicon.ico":  # browsers ask for it unprompted; answering keeps the console clean
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._authorized(query):
            return
        try:
            if url.path == "/":
                page = resources.files("tmt.ui").joinpath("index.html").read_text(encoding="utf-8")
                self._send(page.replace("__VERSION__", __version__).encode("utf-8"), "text/html; charset=utf-8",
                           csp=CSP_PAGE)
            elif url.path == "/api/info":
                self._json({"version": __version__, "cwd": str(Path.cwd()), "example": example_path(),
                            "workspace": self.server.workspace, "guess": guess_workspace()})
            elif url.path == "/api/browse":
                self._json(browse((query.get("path") or [""])[0]))
            elif url.path == "/api/inspect":
                self._json(inspect(self.server, (query.get("path") or [""])[0], (query.get("context") or [""])[0]))
            elif url.path == "/api/explore":
                self._json(explore_api(
                    self.server, (query.get("path") or [""])[0], (query.get("master") or [""])[0],
                    (query.get("related") or ["0"])[0] == "1", (query.get("context") or [""])[0]))
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except InvalidRequest as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    # ---- POST
    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if not self._authorized(parse_qs(url.query)):
            return
        try:
            self._body()
            if url.path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except InvalidRequest as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))


def serve(port: int | None = None, open_browser: bool = True, token: str | None = None, announce: Any = print,
          workspace: str | None = None) -> None:
    """Start the server. ``port=None`` tries :data:`DEFAULT_PORT` and falls back to any free port (for example when
    another user of the same machine already runs one); an explicit port is never replaced."""
    if port is None:
        try:
            server = make_server(DEFAULT_PORT, token, workspace)
        except OSError:
            server = make_server(0, token, workspace)
    else:
        server = make_server(port, token, workspace)
    url = f"http://127.0.0.1:{server.server_port}/?token={server.token}"
    announce(url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
