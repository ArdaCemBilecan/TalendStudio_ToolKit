// Smoke test for the explorer page, driven through the Chrome DevTools Protocol (Node 22+ has WebSocket built in).
// usage: node ui_smoke.mjs <browser exe> <page url> <profile dir>   prints one JSON line.
import { spawn } from "node:child_process";

const [browser, url, profile] = process.argv.slice(2);
const PORT = 9600 + Math.floor(Math.random() * 300);
const proc = spawn(browser, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--window-size=1440,900", "--disable-gpu", "--no-sandbox", "--no-first-run", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const out = { errors: [], tree: false, stats: [], browseOpens: false, welcomeBrowseOpens: false };
let ws;
try {
  let target;
  for (let i = 0; i < 80 && !target; i++) {
    try { target = (await (await fetch(`http://127.0.0.1:${PORT}/json`)).json()).find((t) => t.type === "page"); } catch { await sleep(250); }
  }
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  let id = 0;
  const pending = new Map();
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
    else if (d.method === "Runtime.exceptionThrown") {
      const ex = d.params.exceptionDetails;
      out.errors.push((ex.exception && ex.exception.description) || ex.text);
    }
  };
  const send = (method, params = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (e) => (await send("Runtime.evaluate", { expression: e, awaitPromise: true, returnByValue: true })).result.result?.value;
  const waitFor = async (expr, ms = 30000) => { const t0 = Date.now(); while (Date.now() - t0 < ms) { if (await ev(`!!(${expr})`)) return true; await sleep(150); } return false; };
  const click = async (sel) => {
    const p = await ev(`(() => { const e = document.querySelector('${sel}'); if (!e) return null; const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()`);
    if (!p) return false;
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x: p.x, y: p.y, button: "left", buttons: 1, clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: p.x, y: p.y, button: "left", clickCount: 1 });
    return true;
  };
  await send("Page.enable"); await send("Runtime.enable");
  await send("Page.navigate", { url });
  out.tree = (await waitFor(`document.querySelector('#tree button.name[data-key="job_master_daily"]')`))
    ? await ev(`document.querySelector('#tree button.name[data-key="job_master_daily"]').dataset.key`) : false;
  await waitFor(`document.querySelector('#summary .stat')`);
  out.stats = (await ev(`[...document.querySelectorAll('#summary .stat .n')].map((n) => n.textContent)`)) || [];
  // the toolbar's Browse button in the title bar
  await click("#browse");
  out.browseOpens = await waitFor(`document.getElementById('dlg').open && document.querySelectorAll('#entries button').length > 0`, 8000);
  await ev(`document.getElementById('dlg').close(); true`);
  // the welcome screen's button (shown when no workspace is open)
  await ev(`document.getElementById('explorerEmpty').hidden = false; true`);
  await click("#welcomeBrowse");
  out.welcomeBrowseOpens = await waitFor(`document.getElementById('dlg').open`, 8000);
} catch (e) {
  out.errors.push(String(e));
}
console.log(JSON.stringify(out));
try { ws.close(); } catch { /* ignore */ }
proc.kill();
process.exit(0);
