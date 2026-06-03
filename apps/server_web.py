"""
Olegshifter — сервер с web-дашбордом (FastAPI + uvicorn).

Запуск:
    python apps/server_web.py

Открой http://127.0.0.1:7070 в браузере.

Зависимости: fastapi, uvicorn. См. apps/requirements.txt.

Что делает:
  - Поднимает exit node (multipath WS → SOCKS5-like прокси в интернет).
  - Параллельно — web-интерфейс: старт/стоп, текущие клиенты, конфиг, лог.
  - Конфиг хранится в ~/.olegshifter/server.json.
"""

import os
import sys
import time
import asyncio
import logging
import secrets
from typing import Dict, List, Optional, Tuple
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import uvicorn
    from fastapi import FastAPI, Request, Form, HTTPException, Depends
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
except ImportError as e:
    print("Не хватает зависимостей:", e)
    print("Установи: pip install -r apps/requirements.txt")
    sys.exit(1)

from transport import WebSocketServerTransport, WSConfig, ServerClientSession
from channel_manager import ChannelManager, ChannelManagerConfig
from multipath import SplitMode
from padding import PaddingConfig, PaddingStrategy
from socks5 import ExitNode
from crypto_core import CryptoSession, _derive_keys

from app_config import load_server_config, save_server_config


# --- in-memory лог ---

class RingLog:
    """Кольцевой буфер для логов — видно в UI."""

    def __init__(self, capacity: int = 500) -> None:
        self._buf: deque = deque(maxlen=capacity)

    def add(self, level: str, msg: str) -> None:
        self._buf.append({
            "ts":    time.strftime("%H:%M:%S"),
            "level": level,
            "msg":   msg,
        })

    def snapshot(self) -> List[dict]:
        return list(self._buf)


RING = RingLog(capacity=500)


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        RING.add(record.levelname.lower(), self.format(record))


def _setup_logging() -> logging.Logger:
    log = logging.getLogger("olegshifter.server")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "%(asctime)s [server] %(levelname)s %(message)s", datefmt="%H:%M:%S"
    ))
    log.addHandler(console)

    ring_h = _RingHandler()
    ring_h.setFormatter(fmt)
    log.addHandler(ring_h)

    # ловим и сообщения из ядра
    for name in ("transport", "channel_manager", "socks5", "multipath", "padding"):
        logging.getLogger(name).addHandler(ring_h)
        logging.getLogger(name).setLevel(logging.INFO)

    return log


log = _setup_logging()


# --- собственно сервер (exit node) ---

class ServerCore:
    """
    Воспроизводит логику examples/server.py с возможностью старт/стоп
    и выдачи состояния (список клиентов).
    """

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        self._servers: List[WebSocketServerTransport] = []
        self._pending: Dict[str, Dict[int, ServerClientSession]] = {}
        self._active:  Dict[str, Tuple[ChannelManager, ExitNode, float]] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._started_at: Optional[float] = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def started_at(self) -> Optional[float]:
        return self._started_at

    def clients_snapshot(self) -> List[dict]:
        out = []
        for peer, (mgr, _exit, t0) in self._active.items():
            n_ch = len(getattr(mgr, "_transports", {})) or "-"
            out.append({
                "peer":     peer,
                "since":    time.strftime("%H:%M:%S", time.localtime(t0)),
                "uptime":   int(time.time() - t0),
                "channels": n_ch,
            })
        for peer, pend in self._pending.items():
            out.append({
                "peer":     peer,
                "since":    "—",
                "uptime":   0,
                "channels": f"{len(pend)}/{self._cfg['num_channels']} (ожидает)",
            })
        return out

    async def start(self) -> None:
        if self._running:
            return
        cfg = self._cfg

        for ch_id in range(cfg["num_channels"]):
            port = cfg["base_port"] + ch_id
            wsc = WSConfig(
                host      = cfg["host"],
                port      = port,
                path      = cfg.get("ws_path", "/ws"),
                preshared = cfg["preshared"].encode(),
                tls_cert  = cfg["tls_cert_path"] or None,
                tls_key   = cfg["tls_key_path"]  or None,
            )

            def make_handler(channel_id: int):
                async def handler(session: ServerClientSession):
                    await self._on_client(channel_id, session)
                return handler

            srv = WebSocketServerTransport(wsc, on_client=make_handler(ch_id))
            await srv.start()
            self._servers.append(srv)
            log.info("Канал %d слушает %s:%d", ch_id, cfg["host"], port)

        self._running = True
        self._started_at = time.time()
        log.info("Сервер запущен, %d каналов", cfg["num_channels"])

    async def stop(self) -> None:
        if not self._running:
            return
        for srv in self._servers:
            try:
                await srv.stop()
            except Exception:
                pass
        self._servers.clear()

        for peer, (mgr, _exit, _t) in list(self._active.items()):
            try:
                await mgr.close()
            except Exception:
                pass
        self._active.clear()
        self._pending.clear()
        self._running = False
        self._started_at = None
        log.info("Сервер остановлен")

    async def _on_client(self, channel_id: int, session: ServerClientSession) -> None:
        peer = session.peer[0] if session.peer else "unknown"
        log.info("Клиент %s подключился по каналу %d", peer, channel_id)

        # Реконнект канала к уже работающему exit node — заменяем транспорт,
        # не создаём новую CryptoSession (иначе ключи разойдутся → InvalidTag).
        async with self._lock:
            active_entry = self._active.get(peer)

        if active_entry is not None:
            manager, _exit, _t = active_entry
            log.info("Реконнект канала %d от %s → replace_channel", channel_id, peer)
            await manager.replace_channel(channel_id, session)
            return

        ready: Optional[Dict[int, ServerClientSession]] = None
        async with self._lock:
            self._pending.setdefault(peer, {})[channel_id] = session
            if len(self._pending[peer]) >= self._cfg["num_channels"]:
                ready = self._pending.pop(peer)

        if ready is None:
            while session.is_connected:
                await asyncio.sleep(0.5)
            return

        log.info("Все %d каналов от %s готовы — стартуем exit", self._cfg["num_channels"], peer)
        await self._run_exit(peer, ready)

    async def _run_exit(self, peer: str, transports: Dict[int, ServerClientSession]) -> None:
        cfg = self._cfg
        combined = b"".join(
            transports[k].shared_key for k in sorted(transports)
        )
        c2s, s2c = _derive_keys(combined, cfg["preshared"].encode(),
                                b"session_c2s", b"session_s2c")
        crypto = CryptoSession(s2c, c2s, is_initiator=False)

        mgr_cfg = ChannelManagerConfig(
            split_mode          = SplitMode.ADAPTIVE,
            chunk_size          = 1024,
            padding_cfg         = PaddingConfig(strategy=PaddingStrategy.BIMODAL),
            ping_interval_sec   = 15.0,
            channel_timeout_sec = 60.0,
        )
        manager = ChannelManager(crypto, transports, mgr_cfg)
        exit_node = ExitNode(manager)
        await manager.start()

        self._active[peer] = (manager, exit_node, time.time())
        try:
            while any(t.is_connected for t in transports.values()):
                await asyncio.sleep(1.0)
        finally:
            try:
                await manager.close()
            except Exception:
                pass
            self._active.pop(peer, None)
            log.info("Клиент %s отключился", peer)


# --- web UI ---

app = FastAPI(title="Olegshifter Server")
CFG = load_server_config()
CORE: Optional[ServerCore] = None
CORE_LOCK = asyncio.Lock()

# Если токен не задан в конфиге — генерим и пишем пользователю в stdout,
# чтобы web UI не торчал открытым при запуске на публичном хосте.
if not CFG.get("web_token"):
    CFG["web_token"] = secrets.token_urlsafe(16)
    save_server_config(CFG)
    print(f"[olegshifter] сгенерирован web token: {CFG['web_token']}")


def _require_auth(request: Request) -> None:
    token = request.cookies.get("token") or request.query_params.get("token")
    if token != CFG["web_token"]:
        raise HTTPException(status_code=401, detail="bad token")


PAGE = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Olegshifter — server</title>
<style>
  body { font: 14px system-ui, -apple-system, Segoe UI, sans-serif;
         background: #0e1116; color: #d9dbe0; margin: 0; padding: 24px; }
  h1 { margin: 0 0 16px; font-weight: 600; font-size: 22px; }
  .row { display: flex; gap: 20px; flex-wrap: wrap; }
  .card { background: #171a21; border: 1px solid #232834; border-radius: 8px;
          padding: 16px; flex: 1; min-width: 280px; }
  .pill { display: inline-block; padding: 4px 10px; border-radius: 999px;
          font-size: 12px; }
  .pill.on  { background: #1d3b28; color: #5dd796; }
  .pill.off { background: #3b1d1d; color: #e56767; }
  button { background: #2d6cdf; color: white; border: none; padding: 10px 18px;
           border-radius: 6px; cursor: pointer; font-size: 14px; }
  button.stop { background: #c0392b; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  input, select { background: #0e1116; color: #d9dbe0; border: 1px solid #2a2f3a;
                  padding: 6px 10px; border-radius: 4px; width: 100%;
                  box-sizing: border-box; }
  label { display: block; margin: 8px 0 4px; font-size: 12px; color: #8a8f9a; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #232834;
           font-size: 13px; }
  th { color: #8a8f9a; font-weight: 500; }
  pre { background: #0a0c10; padding: 10px; border-radius: 4px; max-height: 320px;
        overflow: auto; font-size: 12px; margin: 0; }
  .log-info  { color: #d9dbe0; }
  .log-warn  { color: #e0a04d; }
  .log-error { color: #e56767; }
  .log-time  { color: #606670; margin-right: 8px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
</style>
</head>
<body>
  <h1>Olegshifter <span id="status" class="pill off">остановлен</span></h1>

  <div class="row">
    <div class="card">
      <h3 style="margin-top:0">Управление</h3>
      <div id="controls" style="display:flex; gap:10px; margin-bottom:14px;">
        <button id="btn-start">Запустить</button>
        <button id="btn-stop" class="stop">Остановить</button>
      </div>
      <div id="uptime" style="color:#8a8f9a; font-size:12px;"></div>
    </div>

    <div class="card">
      <h3 style="margin-top:0">Клиенты</h3>
      <table>
        <thead><tr><th>Адрес</th><th>Каналов</th><th>Подключён</th><th>Uptime</th></tr></thead>
        <tbody id="clients"><tr><td colspan="4" style="color:#606670">нет</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="row" style="margin-top:20px;">
    <div class="card">
      <h3 style="margin-top:0">Настройки</h3>
      <p style="color:#8a8f9a; font-size:12px; margin-top:0;">
        Применяется при следующем запуске.
      </p>
      <form id="cfg-form">
        <div class="grid2">
          <div><label>host</label><input name="host" value="__HOST__"></div>
          <div><label>base_port</label><input name="base_port" type="number" value="__BASE_PORT__"></div>
          <div><label>num_channels</label><input name="num_channels" type="number" value="__NUM_CHANNELS__"></div>
          <div><label>preshared</label><input name="preshared" value="__PRESHARED__"></div>
          <div><label>ws_path</label><input name="ws_path" value="__WS_PATH__"></div>
          <div><label>tls_cert_path</label><input name="tls_cert_path" value="__TLS_CERT__"></div>
          <div><label>tls_key_path</label><input name="tls_key_path" value="__TLS_KEY__"></div>
        </div>
        <button style="margin-top:14px;" type="submit">Сохранить</button>
        <span id="saved" style="color:#5dd796; margin-left:10px; display:none;">сохранено</span>
      </form>
    </div>

    <div class="card" style="flex-basis:100%;">
      <h3 style="margin-top:0">Лог</h3>
      <pre id="log"></pre>
    </div>
  </div>

<script>
const $ = id => document.getElementById(id);

async function api(path, opts={}) {
  const r = await fetch(path, {credentials: "same-origin", ...opts});
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function refresh() {
  try {
    const s = await api("/api/status");
    $("status").textContent = s.running ? "работает" : "остановлен";
    $("status").className = "pill " + (s.running ? "on" : "off");
    $("btn-start").disabled = s.running;
    $("btn-stop").disabled  = !s.running;
    $("uptime").textContent = s.running
      ? `работает с ${new Date(s.started_at*1000).toLocaleTimeString()}`
      : "";

    const cb = $("clients");
    if (!s.clients.length) {
      cb.innerHTML = '<tr><td colspan="4" style="color:#606670">нет</td></tr>';
    } else {
      cb.innerHTML = s.clients.map(c =>
        `<tr><td>${c.peer}</td><td>${c.channels}</td>`
        + `<td>${c.since}</td><td>${c.uptime}s</td></tr>`
      ).join("");
    }

    const logs = await api("/api/logs");
    $("log").innerHTML = logs.slice(-200).map(l =>
      `<span class="log-time">${l.ts}</span>`
      + `<span class="log-${l.level}">${l.msg}</span>`
    ).join("\\n");
  } catch (e) { /* игнорим — может быть переподключение */ }
}

$("btn-start").onclick = async () => {
  $("btn-start").disabled = true;
  try { await api("/api/start", {method: "POST"}); }
  catch (e) { alert("start: " + e.message); }
  refresh();
};
$("btn-stop").onclick = async () => {
  $("btn-stop").disabled = true;
  try { await api("/api/stop", {method: "POST"}); }
  catch (e) { alert("stop: " + e.message); }
  refresh();
};

$("cfg-form").onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = new URLSearchParams();
  for (const [k, v] of fd.entries()) body.set(k, v);
  await fetch("/api/config", {method: "POST", body, credentials: "same-origin"});
  const s = $("saved");
  s.style.display = "inline";
  setTimeout(() => s.style.display = "none", 1200);
};

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


@app.get("/login", response_class=HTMLResponse)
async def login_page(token: str = ""):
    if token == CFG["web_token"]:
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("token", token, httponly=True, samesite="strict")
        return resp
    return HTMLResponse(
        "<form method='get'><p>token:</p>"
        "<input name='token' autofocus><button>Войти</button></form>"
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    token = request.cookies.get("token") or request.query_params.get("token")
    if token != CFG["web_token"]:
        return RedirectResponse("/login")

    html = (PAGE
        .replace("__HOST__",         CFG["host"])
        .replace("__BASE_PORT__",    str(CFG["base_port"]))
        .replace("__NUM_CHANNELS__", str(CFG["num_channels"]))
        .replace("__PRESHARED__",    CFG["preshared"])
        .replace("__WS_PATH__",      CFG.get("ws_path", "/ws"))
        .replace("__TLS_CERT__",     CFG["tls_cert_path"])
        .replace("__TLS_KEY__",      CFG["tls_key_path"])
    )
    return HTMLResponse(html)


@app.get("/api/status")
async def api_status(_: None = Depends(_require_auth)):
    return {
        "running":    CORE.running if CORE else False,
        "started_at": CORE.started_at if CORE else None,
        "clients":    CORE.clients_snapshot() if CORE else [],
    }


@app.get("/api/logs")
async def api_logs(_: None = Depends(_require_auth)):
    return RING.snapshot()


@app.post("/api/start")
async def api_start(_: None = Depends(_require_auth)):
    global CORE
    async with CORE_LOCK:
        if CORE and CORE.running:
            return {"ok": True, "already": True}
        CORE = ServerCore(CFG)
        await CORE.start()
    return {"ok": True}


@app.post("/api/stop")
async def api_stop(_: None = Depends(_require_auth)):
    global CORE
    async with CORE_LOCK:
        if CORE:
            await CORE.stop()
    return {"ok": True}


@app.post("/api/config")
async def api_config(
    request: Request,
    _: None = Depends(_require_auth),
):
    form = await request.form()
    int_fields = {"base_port", "num_channels"}
    updated = dict(CFG)
    for k, v in form.items():
        if k in int_fields:
            try:
                updated[k] = int(v)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"{k}: не число")
        elif k in CFG:
            updated[k] = v
    save_server_config(updated)
    CFG.update(updated)
    return {"ok": True}


def main() -> None:
    host = CFG["web_host"]
    port = CFG["web_port"]
    print(f"[olegshifter] web UI на http://{host}:{port}")
    print(f"[olegshifter] токен: {CFG['web_token']}")
    print(f"[olegshifter] открыть: http://{host}:{port}/login?token={CFG['web_token']}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
