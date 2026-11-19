"""The web UI server: security first, then behaviour. A real server on a random loopback port, real HTTP."""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from tests.helpers import node, write_job
from tmt import __version__
from tmt.ui import server as ui_server
from tmt.ui.server import UiServer, browse, make_server

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "talend"
TOKEN = "test-token-123"


@pytest.fixture()
def ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[UiServer]:
    monkeypatch.chdir(tmp_path)  # relative --out folders land in the temp dir
    server = make_server(0, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def call(server: UiServer, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
         token: bool = True, host: str | None = None, raw_body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=120)
    hdrs = {"Host": host or f"127.0.0.1:{server.server_port}", **(headers or {})}
    if token:
        hdrs.setdefault("X-TMT-Token", TOKEN)
    payload = raw_body
    if body is not None:
        payload = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=payload, headers=hdrs)
    res = conn.getresponse()
    data = res.read()
    out = (res.status, {k.lower(): v for k, v in res.getheaders()}, data)
    conn.close()
    return out


def jcall(server: UiServer, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
    status, _, data = call(server, method, path, body, **kw)
    return status, json.loads(data)


# ------------------------------------------------------------------ who may talk to it
def test_binds_to_loopback_only(ui: UiServer) -> None:
    assert ui.server_address[0] == "127.0.0.1"


def test_requests_without_or_with_a_wrong_token_are_refused(ui: UiServer) -> None:
    assert call(ui, "GET", "/", token=False)[0] == 403
    assert call(ui, "GET", "/api/info", token=False)[0] == 403
    assert call(ui, "GET", "/api/info", headers={"X-TMT-Token": "nope"}, token=False)[0] == 403
    assert call(ui, "GET", "/api/info", headers={"X-TMT-Token": TOKEN[:-1]}, token=False)[0] == 403
    assert call(ui, "POST", "/api/shutdown", {}, token=False)[0] == 403


def test_the_token_may_come_from_the_query_string_for_links_and_tabs(ui: UiServer) -> None:
    assert call(ui, "GET", f"/api/info?token={TOKEN}", token=False)[0] == 200


def test_a_foreign_host_header_is_refused_dns_rebinding(ui: UiServer) -> None:
    for host in ("evil.example", f"evil.example:{ui.server_port}", f"127.0.0.1.evil.example:{ui.server_port}", ""):
        assert call(ui, "GET", "/api/info", host=host or "x")[0] == 403, host
    assert call(ui, "GET", "/api/info", host=f"localhost:{ui.server_port}")[0] == 200


def test_a_foreign_origin_is_refused_cross_site_requests(ui: UiServer) -> None:
    ok = {"Origin": f"http://127.0.0.1:{ui.server_port}"}
    assert call(ui, "GET", "/api/info", headers=ok)[0] == 200
    for origin in ("http://evil.example", "https://127.0.0.1", "null", f"http://127.0.0.1:{ui.server_port + 1}"):
        assert call(ui, "GET", "/api/info", headers={"Origin": origin})[0] == 403, origin
        assert call(ui, "POST", "/api/shutdown", {}, headers={"Origin": origin})[0] == 403, origin


def test_no_cors_headers_so_other_sites_cannot_read_responses(ui: UiServer) -> None:
    _, headers, _ = call(ui, "GET", "/api/info")
    assert not any(h.startswith("access-control-") for h in headers)


def test_favicon_needs_no_token_and_leaks_nothing(ui: UiServer) -> None:
    status, _, body = call(ui, "GET", "/favicon.ico", token=False)
    assert status == 204 and body == b""


# ------------------------------------------------------------------ the page
def test_page_is_served_with_a_strict_csp_and_no_template_leftovers(ui: UiServer) -> None:
    status, headers, body = call(ui, "GET", f"/?token={TOKEN}", token=False)
    page = body.decode()
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert __version__ in page and "__VERSION__" not in page
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "base-uri 'none'" in csp
    assert headers["x-content-type-options"] == "nosniff" and headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "no-referrer"
    assert not re.search(r'(src|href)="https?://', page) and "<link" not in page  # nothing external


def test_page_script_is_valid_javascript() -> None:
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node is not installed")
    html = (Path(ui_server.__file__).parent / "index.html").read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)  # type: ignore[union-attr]
    tmp = Path(__file__).parent / "_ui_script_check.js"
    tmp.write_text(script, encoding="utf-8")
    try:
        r = subprocess.run([node_bin, "--check", str(tmp)], capture_output=True, text=True)
    finally:
        tmp.unlink()
    assert r.returncode == 0, r.stderr


def test_page_uses_labels_and_roles_for_accessibility() -> None:
    html = (Path(ui_server.__file__).parent / "index.html").read_text(encoding="utf-8")
    assert 'lang="en"' in html and 'name="viewport"' in html and "prefers-color-scheme: dark" in html
    assert 'aria-live="polite"' in html and "<dialog" in html and ":focus-visible" in html
    assert html.count('<label for="path">') == 1
    assert "<button><a" not in html and "<a><button" not in html  # no button inside a link




# ------------------------------------------------------------------ browsing and inspecting
def test_browse_root_lists_drives_or_slash(ui: UiServer) -> None:
    status, data = jcall(ui, "GET", "/api/browse")
    assert status == 200 and data["path"] == "" and data["entries"] and data["parent"] == ""
    assert {s["name"] for s in data["shortcuts"]} >= {"Home", "Current folder"}


def test_browse_lists_only_folders_marks_projects_and_hides_dotfolders(tmp_path: Path) -> None:
    (tmp_path / "proj" / "process").mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    write_job(tmp_path, "j", node("a_1", "tLogRow"))
    data = browse(str(tmp_path))
    names = {e["name"]: e["project"] for e in data["entries"]}
    assert names == {"plain": False, "proj": True}
    assert data["files"] == 1 and data["parent"] == str(tmp_path.parent)


def test_browse_errors_are_clean(ui: UiServer, tmp_path: Path) -> None:
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    status, data = jcall(ui, "GET", f"/api/browse?path={quote(str(f))}")
    assert status == 400 and "not a folder" in data["error"]
    assert jcall(ui, "GET", "/api/browse?path=C:/no/such/folder")[0] == 400


def test_inspect_reports_jobs_for_the_dropdowns(ui: UiServer) -> None:
    status, data = jcall(ui, "GET", f"/api/inspect?path={quote(str(SAMPLES))}")
    assert status == 200 and data["found"] == 17 and data["failures"] == [] and data["tools"] == ["talend"]
    job = next(j for j in data["jobs"] if j["key"] == "job_join_lookup")
    assert job["level"] == "medium" and job["score"] == 12
    assert jcall(ui, "GET", "/api/inspect?path=C:/no/such/folder")[0] == 400
    assert jcall(ui, "GET", "/api/inspect")[0] == 400


def test_inspect_empty_folder_says_zero(ui: UiServer, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    status, data = jcall(ui, "GET", f"/api/inspect?path={quote(str(empty))}")
    assert status == 200 and data["found"] == 0 and data["jobs"] == []


# ------------------------------------------------------------------ running things


















# ------------------------------------------------------------------ serving files safely








# ------------------------------------------------------------------ the remaining endpoints


def test_body_validation(ui: UiServer) -> None:
    assert call(ui, "POST", "/api/shutdown", raw_body=b'{"action": "parsers"}', headers={"Content-Type": "text/plain"})[0] == 400
    assert call(ui, "POST", "/api/shutdown", raw_body=b"{not json", headers={"Content-Type": "application/json"})[0] == 400
    assert call(ui, "POST", "/api/shutdown", raw_body=b"[1, 2]", headers={"Content-Type": "application/json"})[0] == 400
    big = b'{"x": "' + b"a" * 70_000 + b'"}'
    assert call(ui, "POST", "/api/shutdown", raw_body=big, headers={"Content-Type": "application/json"})[0] == 400


def test_unknown_routes_are_404(ui: UiServer) -> None:
    assert call(ui, "GET", "/api/nope")[0] == 404 and call(ui, "POST", "/api/nope", {})[0] == 404




def test_shutdown_stops_the_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    server = make_server(0, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert jcall(server, "POST", "/api/shutdown", {})[0] == 200
    thread.join(timeout=10)
    assert not thread.is_alive()
    server.server_close()


# ------------------------------------------------------------------ the command: tmt ui
def test_tmt_ui_starts_prints_a_private_url_and_serves_it() -> None:
    proc = subprocess.Popen([sys.executable, "-m", "tmt", "ui", "--no-browser"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    try:
        url = None
        deadline = time.time() + 30
        while time.time() < deadline and url is None:
            line = proc.stdout.readline()  # type: ignore[union-attr]
            m = re.search(r"http://127\.0\.0\.1:(\d+)/\?token=([A-Za-z0-9_-]+)", line)
            if m:
                url = m
        assert url is not None, "tmt ui did not print its address"
        assert len(url.group(2)) >= 24  # a long random token
        conn = http.client.HTTPConnection("127.0.0.1", int(url.group(1)), timeout=15)
        conn.request("GET", f"/?token={url.group(2)}")
        assert conn.getresponse().status == 200
        conn = http.client.HTTPConnection("127.0.0.1", int(url.group(1)), timeout=15)
        conn.request("GET", "/")
        assert conn.getresponse().status == 403
    finally:
        proc.terminate()
        proc.wait(timeout=15)


def test_tmt_ui_help_and_python_dash_m() -> None:
    r = subprocess.run([sys.executable, "-m", "tmt", "ui", "--help"], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0 and "--no-browser" in r.stdout and "127.0.0.1" in r.stdout
    v = subprocess.run([sys.executable, "-m", "tmt", "--version"], capture_output=True, text=True, encoding="utf-8")
    assert v.stdout.strip() == f"tmt {__version__}"


# ------------------------------------------------------------------ the dependency explorer
DEMO = Path(__file__).resolve().parents[2] / "samples" / "talend_demo"


def explore_url(master: str = "", related: bool = False, path: Path = DEMO) -> str:
    return f"/api/explore?path={quote(str(path))}&master={quote(master)}&related={'1' if related else '0'}"


def test_explore_returns_summary_details_and_interactive_svg(ui: UiServer) -> None:
    status, data = jcall(ui, "GET", explore_url("job_master_daily"))
    assert status == 200 and data["master"] == "job_master_daily"
    s = data["summary"]
    assert (s["sources"], s["targets"]) == (12, 10)
    assert [(c["name"], c["uses"]) for c in s["connections"]] == [("ODS_DB", 10), ("AKY_DB", 2)]
    assert 'data-node="job:job_stage_orders"' in data["svg"] and "job:job_stage_orders" in data["details"]
    assert data["masters"][0]["key"] == "job_master_daily"


def test_explore_whole_workspace_and_related_toggle(ui: UiServer) -> None:
    _, whole = jcall(ui, "GET", explore_url())
    assert whole["summary"]["master"] is None and whole["summary"]["jobs"] == 7
    _, plain = jcall(ui, "GET", explore_url("job_master_daily"))
    _, more = jcall(ui, "GET", explore_url("job_master_daily", related=True))
    assert more["summary"]["jobs"] == plain["summary"]["jobs"] + 1


def test_explore_rejects_unknown_master_and_missing_folder(ui: UiServer, tmp_path: Path) -> None:
    status, data = jcall(ui, "GET", explore_url("no_such_job"))
    assert status == 400 and "not found" in data["error"]
    status, _ = jcall(ui, "GET", explore_url(path=tmp_path / "missing"))
    assert status == 400


def test_explore_empty_folder_says_so(ui: UiServer, tmp_path: Path) -> None:
    status, data = jcall(ui, "GET", explore_url(path=tmp_path))
    assert status == 200 and data["svg"] is None and "No job files" in data["message"]


def test_explore_needs_the_token(ui: UiServer) -> None:
    status, _, _ = call(ui, "GET", explore_url("job_master_daily"), token=False)
    assert status in (401, 403)


def test_explore_refuses_to_draw_a_huge_workspace(ui: UiServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_server, "MAX_WHOLE_GRAPH", 3)
    _, data = jcall(ui, "GET", explore_url())
    assert data["svg"] is None and "Choose a master job" in data["message"] and data["summary"]["jobs"] == 7
    _, one = jcall(ui, "GET", explore_url("job_master_daily"))
    assert one["svg"]  # a master job is always drawn


def test_explore_scan_is_cached_until_a_file_changes(ui: UiServer, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = ui_server.scan
    monkeypatch.setattr(ui_server, "scan", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    jcall(ui, "GET", explore_url("job_master_daily"))
    jcall(ui, "GET", explore_url("job_master_weekly"))
    assert len(calls) == 1


def test_example_path_prefers_the_demo_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "samples" / "talend").mkdir(parents=True)
    assert ui_server.example_path() == str(tmp_path / "samples" / "talend")
    (tmp_path / "samples" / "talend_demo").mkdir()
    assert ui_server.example_path() == str(tmp_path / "samples" / "talend_demo")



# ------------------------------------------------------------------ read-only by design
def test_info_endpoint(ui: UiServer) -> None:
    status, data = jcall(ui, "GET", "/api/info")
    assert status == 200 and data["version"] == __version__
    assert data["example"] is None  # the temp cwd has no samples folder


@pytest.mark.parametrize("route", ["/api/run", "/api/preview", "/api/open-folder", "/api/file", "/api/actions"])
def test_the_server_cannot_run_commands_or_serve_files(ui: UiServer, route: str) -> None:
    assert call(ui, "GET", route)[0] == 404
    assert call(ui, "POST", route, {"action": "parsers", "params": {}})[0] == 404


def test_the_server_module_never_starts_a_process() -> None:
    text = Path(ui_server.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in text and "os.system" not in text and "startfile" not in text


# ------------------------------------------------------------------ one copy per user
def make_ws(root: Path) -> Path:
    (root / "proj" / "process").mkdir(parents=True)
    return root


def test_guess_workspace_prefers_the_environment_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "anywhere"
    ws.mkdir()
    monkeypatch.setenv("TMT_WORKSPACE", str(ws))
    assert ui_server.guess_workspace(tmp_path / "home") == str(ws)
    monkeypatch.setenv("TMT_WORKSPACE", str(tmp_path / "missing"))  # a wrong value is ignored, not trusted
    assert ui_server.guess_workspace(tmp_path / "home") is None


def test_guess_workspace_finds_the_usual_places(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMT_WORKSPACE", raising=False)
    home = tmp_path / "home"
    assert ui_server.guess_workspace(home) is None
    make_ws(home / "Desktop" / "Talend_WS")
    assert ui_server.guess_workspace(home) == str(home / "Desktop" / "Talend_WS")
    make_ws(home / "workspace")  # the Studio default wins over the desktop folder
    assert ui_server.guess_workspace(home) == str(home / "workspace")


def test_guess_workspace_ignores_folders_without_a_talend_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMT_WORKSPACE", raising=False)
    (tmp_path / "workspace" / "docs").mkdir(parents=True)
    assert ui_server.guess_workspace(tmp_path) is None


def test_info_reports_the_given_workspace_and_the_guess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    server = make_server(0, TOKEN, workspace=str(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, data = jcall(server, "GET", "/api/info")
        assert data["workspace"] == str(tmp_path) and "guess" in data
    finally:
        server.shutdown()
        server.server_close()


def test_serve_falls_back_to_a_free_port_when_the_default_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    monkeypatch.setattr(ui_server, "DEFAULT_PORT", blocker.getsockname()[1])
    seen: list[str] = []

    def stop(url: str) -> None:
        seen.append(url)
        raise KeyboardInterrupt  # announce() is called before serve_forever; this ends serve() cleanly... via finally

    try:
        with pytest.raises(KeyboardInterrupt):
            ui_server.serve(open_browser=False, announce=stop)
    finally:
        blocker.close()
    assert seen and f":{ui_server.DEFAULT_PORT}/" not in seen[0]


def test_cli_ui_rejects_a_workspace_that_is_not_a_folder(tmp_path: Path) -> None:
    r = subprocess.run([sys.executable, "-m", "tmt", "ui", str(tmp_path / "nope"), "--no-browser"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 2 and "is not a folder" in r.stderr


def test_a_second_server_cannot_take_a_port_that_is_in_use(ui: UiServer) -> None:
    # another user on the same machine must get a different port, never the first user's server
    with pytest.raises(OSError):
        make_server(ui.server_port, "another-token")


# ------------------------------------------------------------------ the page really starts (needs a browser + node)
def _find_browser() -> str | None:
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    for path in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if Path(path).exists():
            return path
    return None


def test_the_page_starts_and_its_buttons_work_in_a_real_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A script that parses but throws while starting (a typo in a variable) leaves every button dead, and
    ``node --check`` cannot see that. This loads the page in a real browser, clicks Browse and opens a job."""
    node_bin, browser = shutil.which("node"), _find_browser()
    if node_bin is None or browser is None or not DEMO.is_dir():
        pytest.skip("needs node and a Chromium-based browser")
    monkeypatch.chdir(tmp_path)
    server = make_server(0, TOKEN, workspace=str(DEMO))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        script = Path(__file__).parent / "ui_smoke.mjs"
        r = subprocess.run([node_bin, str(script), browser, f"http://127.0.0.1:{server.server_port}/?token={TOKEN}",
                            str(tmp_path / "profile")], capture_output=True, text=True, encoding="utf-8", timeout=120)
    finally:
        server.shutdown()
        server.server_close()
    assert r.returncode == 0, r.stdout + r.stderr
    result = json.loads(r.stdout)
    assert result["errors"] == []
    assert result["tree"] and "job_master_daily" in result["tree"]
    assert result["stats"] == ["12", "10"]
    assert result["browseOpens"] and result["welcomeBrowseOpens"], result
