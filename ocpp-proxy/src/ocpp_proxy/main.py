import asyncio
import base64
import csv
import io
import json
import logging
import os
import time
import uuid

import websockets
from aiohttp import WSCloseCode, web

from .config import Config
from .logger import EventLogger

_LOGGER = logging.getLogger(__name__)

_charger_info: dict = {
    "connected": False,
    "vendor": "unknown",
    "model": "unknown",
    "last_id_tag": "",
    "last_status": "Available",
    "evcc_status": "A",
    "firmware": "unknown",
    "serial": "unknown",
}

_meter_values: dict = {
    "energy_wh": 0.0,
    "power_w": 0.0,
    "current_l1": 0.0,
    "current_l2": 0.0,
    "current_l3": 0.0,
    "voltage_l1": 0.0,
    "voltage_l2": 0.0,
    "voltage_l3": 0.0,
    "timestamp": None,
}

_last_session: dict = {
    "id_tag": "",
    "transaction_id": 0,
    "start_time": "",
    "stop_time": "",
    "meter_start_wh": 0.0,
    "meter_stop_wh": 0.0,
    "energy_wh": 0.0,
    "stop_reason": "",
}

_data_transfer_log: list = []

_active_charger_ws = None
_pending_responses: dict = {}
_auto_throttle: bool = True
_min_current: int = 6


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


def _sniff(raw: str) -> bool:
    """Sniff OCPP messages. Returns True if StartTransaction detected."""
    try:
        msg = json.loads(raw)
        if not isinstance(msg, list) or len(msg) < 3:
            return False
        msg_type = msg[0]
        action = msg[2] if len(msg) > 2 else ""
        payload = msg[3] if len(msg) > 3 else {}

        if msg_type == 3:
            msg_id = msg[1]
            if msg_id in _pending_responses:
                _pending_responses[msg_id]["response"] = msg
                _pending_responses[msg_id]["event"].set()
            return False

        if action in ("Authorize", "StartTransaction"):
            id_tag = payload.get("idTag") or payload.get("id_tag")
            if id_tag:
                _charger_info["last_id_tag"] = id_tag
                _LOGGER.info("Captured idTag=%s from %s", id_tag, action)
            if action == "StartTransaction":
                _last_session["id_tag"] = id_tag
                _last_session["start_time"] = payload.get("timestamp")
                _last_session["meter_start_wh"] = payload.get("meterStart")
                _last_session["stop_time"] = None
                _last_session["stop_reason"] = None
                _last_session["energy_wh"] = None
                return True

        if action == "BootNotification":
            _charger_info["vendor"] = payload.get("chargePointVendor")
            _charger_info["model"] = payload.get("chargePointModel")
            _charger_info["firmware"] = payload.get("firmwareVersion")
            _charger_info["serial"] = payload.get("chargePointSerialNumber")

        if action == "StatusNotification":
            ocpp_status = payload.get("status", "")
            _charger_info["last_status"] = ocpp_status
            _charger_info["evcc_status"] = {
                "Available": "A",
                "Preparing": "B",
                "Charging": "C",
                "SuspendedEV": "B",
                "SuspendedEVSE": "B",
                "Finishing": "B",
                "Faulted": "F",
                "Unavailable": "A",
            }.get(ocpp_status, "A")

        if action == "StopTransaction":
            meter_stop = payload.get("meterStop")
            _last_session["stop_time"] = payload.get("timestamp")
            _last_session["meter_stop_wh"] = meter_stop
            _last_session["stop_reason"] = payload.get("reason", "Unknown")
            if _last_session["meter_start_wh"] is not None and meter_stop is not None:
                _last_session["energy_wh"] = meter_stop - _last_session["meter_start_wh"]
            id_tag = payload.get("idTag") or payload.get("id_tag") or _last_session.get("id_tag")
            _last_session["id_tag"] = id_tag
            _LOGGER.info(
                "StopTransaction: idTag=%s energy=%s Wh reason=%s",
                id_tag,
                _last_session["energy_wh"],
                _last_session["stop_reason"],
            )

        if action == "DataTransfer":
            entry = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "vendorId": payload.get("vendorId"),
                "messageId": payload.get("messageId"),
                "data": payload.get("data"),
            }
            _data_transfer_log.append(entry)
            if len(_data_transfer_log) > 100:
                _data_transfer_log.pop(0)
            _LOGGER.info("DataTransfer: %s", entry)

        if action == "MeterValues":
            for mv in payload.get("meterValue", []):
                ts = mv.get("timestamp")
                if ts:
                    _meter_values["timestamp"] = ts
                for sv in mv.get("sampledValue", []):
                    measurand = sv.get("measurand", "")
                    phase = sv.get("phase", "")
                    value = sv.get("value")
                    if value is None:
                        continue
                    if measurand == "Energy.Active.Import.Register":
                        _meter_values["energy_wh"] = float(value)
                    elif measurand == "Power.Active.Import":
                        _meter_values["power_w"] = float(value)
                    elif measurand == "Current.Import" and phase == "L1":
                        _meter_values["current_l1"] = float(value)
                    elif measurand == "Current.Import" and phase == "L2":
                        _meter_values["current_l2"] = float(value)
                    elif measurand == "Current.Import" and phase == "L3":
                        _meter_values["current_l3"] = float(value)
                    elif measurand == "Voltage" and phase == "L1-N":
                        _meter_values["voltage_l1"] = float(value)
                    elif measurand == "Voltage" and phase == "L2-N":
                        _meter_values["voltage_l2"] = float(value)
                    elif measurand == "Voltage" and phase == "L3-N":
                        _meter_values["voltage_l3"] = float(value)
        return False
    except Exception:
        return False


async def _connect_upstream(url: str):
    return await websockets.connect(
        url,
        subprotocols=["ocpp1.6"],
        ping_interval=None,
        ping_timeout=None,
    )


async def charger_handler(request: web.Request) -> web.WebSocketResponse:
    global _active_charger_ws
    config = request.app["config"]

    if config.charger_password:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                _, _, provided_password = decoded.partition(":")
            except Exception:
                provided_password = ""
        else:
            provided_password = ""
        if provided_password != config.charger_password:
            _LOGGER.warning("Charger connection rejected: wrong password from %s", request.remote)
            return web.Response(status=401, text="Unauthorized")

    ws = web.WebSocketResponse(protocols=("ocpp1.6", "ocpp2.0.1"))
    await ws.prepare(request)

    upstream_url = config.upstream_url or None

    _charger_info["connected"] = True
    _active_charger_ws = ws
    _LOGGER.info("Charger connected. Upstream: %s", upstream_url or "none")

    upstream_ws = None
    if upstream_url:
        try:
            upstream_ws = await _connect_upstream(upstream_url)
            _LOGGER.info("Connected to upstream %s", upstream_url)
        except Exception:
            _LOGGER.exception("Failed to connect to upstream")

    pending_charger_msgs: asyncio.Queue = asyncio.Queue()

    async def throttle_to_zero():
        await asyncio.sleep(1)
        try:
            _LOGGER.info("AUTO-THROTTLE: setting current to 0A")
            await _send_to_charger(
                "SetChargingProfile",
                {
                    "connectorId": 1,
                    "csChargingProfiles": {
                        "chargingProfileId": 2,
                        "stackLevel": 1,
                        "chargingProfilePurpose": "TxProfile",
                        "chargingProfileKind": "Absolute",
                        "chargingSchedule": {
                            "chargingRateUnit": "A",
                            "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 0}],
                        },
                    },
                },
            )
            _LOGGER.info("AUTO-THROTTLE: charger set to 0A")
        except Exception:
            _LOGGER.exception("AUTO-THROTTLE: failed to set 0A")

    async def charger_to_upstream():
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                break
            _LOGGER.info("CHARGER -> UPSTREAM: %s", msg.data)
            is_start = _sniff(msg.data)
            if is_start and _auto_throttle:
                asyncio.create_task(throttle_to_zero())
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
                _sniff(raw)
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
        _active_charger_ws = None
        await pending_charger_msgs.put(None)
        if upstream_ws:
            await upstream_ws.close()
        await ws.close(code=WSCloseCode.GOING_AWAY)
    return ws


async def _send_to_charger(action: str, payload: dict, timeout: float = 10.0) -> dict:
    if not _active_charger_ws:
        raise RuntimeError("No charger connected")
    msg_id = str(uuid.uuid4())
    msg = json.dumps([2, msg_id, action, payload])
    event = asyncio.Event()
    _pending_responses[msg_id] = {"event": event, "response": None}
    try:
        await _active_charger_ws.send_str(msg)
        _LOGGER.info("INJECTED -> CHARGER: %s", msg)
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return _pending_responses[msg_id]["response"]
    finally:
        _pending_responses.pop(msg_id, None)


async def command_handler(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    action = body.get("action")
    payload = body.get("payload", {})

    if not action:
        return web.json_response({"error": "action required"}, status=400)

    if not _active_charger_ws:
        return web.json_response({"error": "no charger connected"}, status=503)

    try:
        response = await _send_to_charger(action, payload)
        return web.json_response({"action": action, "response": response})
    except asyncio.TimeoutError:
        return web.json_response({"error": "charger did not respond"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def enable_handler(request: web.Request) -> web.Response:
    enable = request.match_info.get("enable", "true").lower() == "true"
    if not _active_charger_ws:
        return web.json_response({"error": "no charger connected"}, status=503)
    try:
        min_current = request.app["config"].min_current
        limit = min_current if enable else 0
        payload = {
            "connectorId": 1,
            "csChargingProfiles": {
                "chargingProfileId": 2,
                "stackLevel": 1,
                "chargingProfilePurpose": "TxProfile",
                "chargingProfileKind": "Absolute",
                "chargingSchedule": {
                    "chargingRateUnit": "A",
                    "chargingSchedulePeriod": [{"startPeriod": 0, "limit": limit}],
                },
            },
        }
        response = await _send_to_charger("SetChargingProfile", payload)
        return web.json_response(
            {
                "action": "SetChargingProfile",
                "enable": enable,
                "limit_amps": limit,
                "response": response,
            }
        )
    except asyncio.TimeoutError:
        return web.json_response({"error": "charger did not respond"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def maxcurrent_handler(request: web.Request) -> web.Response:
    try:
        amps = int(request.match_info["amps"])
    except (KeyError, ValueError):
        return web.json_response({"error": "amps required"}, status=400)
    if not _active_charger_ws:
        return web.json_response({"error": "no charger connected"}, status=503)
    try:
        payload = {
            "connectorId": 1,
            "csChargingProfiles": {
                "chargingProfileId": 1,
                "stackLevel": 0,
                "chargingProfilePurpose": "TxDefaultProfile",
                "chargingProfileKind": "Absolute",
                "chargingSchedule": {
                    "chargingRateUnit": "A",
                    "chargingSchedulePeriod": [{"startPeriod": 0, "limit": amps}],
                },
            },
        }
        response = await _send_to_charger("SetChargingProfile", payload)
        return web.json_response(
            {"action": "SetChargingProfile", "amps": amps, "response": response}
        )
    except asyncio.TimeoutError:
        return web.json_response({"error": "charger did not respond"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


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
            "upstream": request.app["config"].upstream_url or None,
        }
    )


async def charger_info_handler(request: web.Request) -> web.Response:
    return web.json_response(_charger_info.copy())


async def meter_values_handler(request: web.Request) -> web.Response:
    return web.json_response(_meter_values.copy())


async def last_session_handler(request: web.Request) -> web.Response:
    return web.json_response(_last_session.copy())


async def data_transfer_handler(request: web.Request) -> web.Response:
    return web.json_response(_data_transfer_log[-20:])


async def welcome_handler(_request: web.Request) -> web.Response:
    html = """<!DOCTYPE html>
<html><head><title>OCPP Sniffer</title></head><body>
<h1>OCPP Sniffer</h1>
<h2>Read endpoints</h2>
<ul>
  <li><a href="/charger_info">/charger_info</a> - idTag, status, vendor, firmware</li>
  <li><a href="/meter_values">/meter_values</a> - L1/L2/L3 voltage, current, power, energy</li>
  <li><a href="/last_session">/last_session</a> - last completed charging session</li>
  <li><a href="/data_transfer">/data_transfer</a> - vendor DataTransfer messages (last 20)</li>
  <li><a href="/status">/status</a> - upstream URL and connection state</li>
  <li><a href="/sessions">/sessions</a> - all sessions JSON</li>
  <li><a href="/sessions.csv">/sessions.csv</a> - all sessions CSV</li>
</ul>
<h2>Command endpoints (POST)</h2>
<ul>
  <li>POST /enable/true - resume charging (SetChargingProfile min_current A)</li>
  <li>POST /enable/false - pause charging (SetChargingProfile 0A)</li>
  <li>POST /maxcurrent/{amps} - set max current (SetChargingProfile)</li>
  <li>POST /command - raw OCPP command {"action":"...","payload":{...}}</li>
</ul>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


async def init_app() -> web.Application:
    global _auto_throttle, _min_current
    config = Config()
    _auto_throttle = config.auto_throttle
    _min_current = config.min_current
    if _auto_throttle:
        _LOGGER.info(
            "Auto-throttle enabled: charger set to 0A on StartTransaction, evcc controls via /enable"
        )
    if config.charger_password:
        _LOGGER.info("Charger password configured: only authenticated chargers accepted")
    else:
        _LOGGER.warning("No charger password set: any charger can connect")

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
            web.get("/meter_values", meter_values_handler),
            web.get("/last_session", last_session_handler),
            web.get("/data_transfer", data_transfer_handler),
            web.post("/enable/{enable}", enable_handler),
            web.post("/maxcurrent/{amps}", maxcurrent_handler),
            web.post("/command", command_handler),
        ]
    )
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = asyncio.run(init_app())
    web.run_app(app, port=int(os.getenv("PORT", 9000)))


if __name__ == "__main__":
    main()
