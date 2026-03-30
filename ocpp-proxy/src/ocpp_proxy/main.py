import asyncio
import csv
import io
import json
import logging
import os

import websockets
from aiohttp import WSCloseCode, web

from .config import Config
from .logger import EventLogger

_LOGGER = logging.getLogger(__name__)

_charger_info: dict = {
    "connected": False,
    "vendor": None,
    "model": None,
    "last_id_tag": None,
    "last_status": None,
}


@web.middleware
async def log_all_requests(request, handler):
    real_ip = request.headers.get(
        "CF-Connecting-IP",
        request.headers.get("X-Forwarded-For", request.remote),
    )
    _LOGGER.info(
        "HTTP %s %s from %s WS-Proto=%s UA=%s",
        request.method,
        request.path_qs,
        real_ip,
        request.headers.get("Sec-WebSocket-Protocol", ""),
        request.headers.get("User-Agent", ""),
    )
    return await handler(request)


def _sniff(raw: str) -> None:
    try:
        msg = json.loads(raw)
        if not isinstance(msg, list) or len(msg) < 3:
            return
        action = msg[2] if len(msg) > 2 else ""
        payload = msg[3] if len(msg) > 3 else {}
        if action in ("Authorize", "StartTransaction"):
            id_tag = payload.get("idTag") or payload.get("id_tag")
            if id_tag:
                _charger_info["last_id_tag"] = id_tag
                _LOGGER.info("Captured idTag=%s from %s", id_tag, action)
        if action == "BootNotification":
            _charger_info["vendor"] = payload.get("chargePointVendor")
            _charger_info["model"] = payload.get("chargePointModel")
        if action == "StatusNotification":
            _charger_info["last_status"] = payload.get("status")
    except Exception:
        pass


async def _connect_upstream(url: str):
    return await websockets.connect(
        url,
        subprotocols=["ocpp1.6"],
        ping_interval=None,
        ping_timeout=None,
    )


async def charger_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(protocols=("ocpp1.6", "ocpp2.0.1"))
    await ws.prepare(request)

    config = request.app["config"]
    upstream_url = config.ocpp_services[0].get("url") if config.ocpp_services else None

    _charger_info["connected"] = True
    _LOGGER.info("Charger connected. Upstream: %s", upstream_url or "none")

    upstream_ws = None
    if upstream_url:
        try:
            upstream_ws = await _connect_upstream(upstream_url)
            _LOGGER.info("Connected to upstream %s", upstream_url)
        except Exception:
            _LOGGER.exception("Failed to connect to upstream")

    pending_charger_msgs: asyncio.Queue = asyncio.Queue()

    async def charger_to_upstream():
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                break
            _LOGGER.info("CHARGER -> UPSTREAM: %s", msg.data)
            _sniff(msg.data)
            await pending_charger_msgs.put(msg.data)

    async def upstream_relay():
        nonlocal upstream_ws
        while True:
            raw = await pending_charger_msgs.get()
            if raw is None:
                break
            if not upstream_ws or upstream_ws.state.name not in ("OPEN",):
                if upstream_url:
                    try:
                        _LOGGER.info("Reconnecting to upstream %s", upstream_url)
                        upstream_ws = await _connect_upstream(upstream_url)
                        _LOGGER.info("Reconnected to upstream %s", upstream_url)
                        asyncio.create_task(upstream_to_charger_loop(upstream_ws))
                    except Exception:
                        _LOGGER.exception("Failed to reconnect to upstream")
                        continue
            try:
                await upstream_ws.send(raw)
            except Exception:
                _LOGGER.exception("Failed to send to upstream")

    async def upstream_to_charger_loop(u_ws):
        try:
            async for raw in u_ws:
                _LOGGER.info("UPSTREAM -> CHARGER: %s", raw)
                try:
                    await ws.send_str(raw)
                except Exception:
                    _LOGGER.exception("Failed to send to charger")
                    break
        except Exception:
            _LOGGER.info("Upstream connection closed")

    try:
        if upstream_ws:
            asyncio.create_task(upstream_to_charger_loop(upstream_ws))
        await asyncio.gather(charger_to_upstream(), upstream_relay())
    except Exception:
        _LOGGER.exception("Proxy error")
    finally:
        _charger_info["connected"] = False
        await pending_charger_msgs.put(None)
        if upstream_ws:
            await upstream_ws.close()
        await ws.close(code=WSCloseCode.GOING_AWAY)
    return ws


async def sessions_json(request: web.Request) -> web.Response:
    sessions = request.app["event_logger"].get_sessions()
    return web.json_response(sessions)


async def sessions_csv(request: web.Request) -> web.Response:
    sessions = request.app["event_logger"].get_sessions()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "backend_id", "duration_s", "energy_kwh", "revenue", "id_tag"])
    for s in sessions:
        writer.writerow(
            [
                s["timestamp"],
                s["backend_id"],
                s["duration_s"],
                s["energy_kwh"],
                s["revenue"],
                s.get("id_tag", ""),
            ]
        )
    return web.Response(text=output.getvalue(), content_type="text/csv")


async def status_handler(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "charger_connected": _charger_info["connected"],
            "charger_vendor": _charger_info["vendor"],
            "charger_model": _charger_info["model"],
            "last_id_tag": _charger_info["last_id_tag"],
            "last_status": _charger_info["last_status"],
            "upstream": request.app["config"].ocpp_services[0].get("url")
            if request.app["config"].ocpp_services
            else None,
        }
    )


async def charger_info_handler(request: web.Request) -> web.Response:
    return web.json_response(_charger_info.copy())


async def welcome_handler(_request: web.Request) -> web.Response:
    html = """<!DOCTYPE html>
<html><head><title>OCPP Transparent Proxy</title></head><body>
<h1>OCPP Transparent Proxy</h1>
<ul>
  <li><a href="/charger_info">/charger_info</a> - last idTag and charger state</li>
  <li><a href="/status">/status</a> - upstream and charger status</li>
  <li><a href="/sessions">/sessions</a> - completed sessions (JSON)</li>
  <li><a href="/sessions.csv">/sessions.csv</a> - completed sessions (CSV)</li>
</ul>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


async def init_app() -> web.Application:
    config = Config()

    app = web.Application(middlewares=[log_all_requests])
    app["config"] = config
    app["event_logger"] = EventLogger(db_path=os.getenv("LOG_DB_PATH", "usage_log.db"))

    app.add_routes(
        [
            web.get("/", welcome_handler),
            web.get("/charger", charger_handler),
            web.get("/charger/{charger_id}", charger_handler),
            web.get("/sessions", sessions_json),
            web.get("/sessions.csv", sessions_csv),
            web.get("/status", status_handler),
            web.get("/charger_info", charger_info_handler),
        ]
    )
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = asyncio.run(init_app())
    web.run_app(app, port=int(os.getenv("PORT", 9000)))


if __name__ == "__main__":
    main()
