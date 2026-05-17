import asyncio
import base64
import csv
import io
import json
import logging
import os
import time
import uuid

import requests
import websockets
from aiohttp import WSCloseCode, web

from .config import Config
from .logger import EventLogger


# --- Wallbox eco_mode toggle via Home Assistant API ---
# eco_mode must be OFF for OCPP charging to work.
# We disable it before EVCC starts charging, re-enable when session ends.
# Uses the HA Supervisor API (SUPERVISOR_TOKEN) to toggle the select entity.

_HA_URL = "http://supervisor/core/api"
_HA_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")
_ECO_MODE_ENTITY = ""  # Set from config at startup
_ECO_MODE_MANAGEMENT = True  # Set from config at startup
_ECO_MODE_ENABLED = True  # Track current eco_mode state
_LOG_NOISE = False  # Log [...] noise lines (meter_values, heartbeats, acks)
_eco_mode_bounce_task: asyncio.Task | None = None  # Bounce guard task


def _get_eco_mode_state() -> str:
    """Read the current eco_mode entity state from HA."""
    try:
        resp = requests.get(
            f"{_HA_URL}/states/{_ECO_MODE_ENTITY}",
            headers={
                "Authorization": f"Bearer {_HA_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("state", "unknown")
    except Exception as e:
        _LOGGER.error("[LOG] Failed to read eco_mode state: %s", e)
    return "unknown"


async def set_eco_mode(enabled: bool):
    """Toggle Wallbox eco_mode via HA select entity. Verifies the change took effect."""
    global _ECO_MODE_ENABLED
    if not _ECO_MODE_MANAGEMENT or not _ECO_MODE_ENTITY:
        return True
    # Change 4: verify actual HA state before skipping
    loop = asyncio.get_event_loop()
    actual = await loop.run_in_executor(None, _get_eco_mode_state)
    actual_is_on = (actual == "eco_mode")
    if enabled == actual_is_on:
        _ECO_MODE_ENABLED = enabled
        _LOGGER.info("[LOG] eco_mode already %s (verified from HA), skipping", "ON" if enabled else "OFF")
        return True

    option = "eco_mode" if enabled else "off"

    def _call_ha():
        resp = requests.post(
            f"{_HA_URL}/services/select/select_option",
            headers={
                "Authorization": f"Bearer {_HA_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "entity_id": _ECO_MODE_ENTITY,
                "option": option,
            },
            timeout=10,
        )
        return resp.status_code in (200, 201)

    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, _call_ha)
    if not success:
        _LOGGER.error("[LOG] Failed to set eco_mode to %s via HA (API call failed)", "ON" if enabled else "OFF")
        return False

    _LOGGER.info("[LOG] eco_mode set to %s via HA, waiting to verify...", "ON" if enabled else "OFF")

    # Verify after a short delay that the state actually changed
    await asyncio.sleep(5)
    actual = await loop.run_in_executor(None, _get_eco_mode_state)
    if actual == option:
        _ECO_MODE_ENABLED = enabled
        _LOGGER.info("[LOG] eco_mode VERIFIED: %s (entity state: %s)", "ON" if enabled else "OFF", actual)
        return True

    # Retry once
    _LOGGER.warning("[LOG] eco_mode NOT verified (expected %s, got %s). Retrying...", option, actual)
    success = await loop.run_in_executor(None, _call_ha)
    if not success:
        _LOGGER.error("[LOG] eco_mode retry failed (API call failed)")
        return False

    await asyncio.sleep(5)
    actual = await loop.run_in_executor(None, _get_eco_mode_state)
    if actual == option:
        _ECO_MODE_ENABLED = enabled
        _LOGGER.info("[LOG] eco_mode VERIFIED on retry: %s (entity state: %s)", "ON" if enabled else "OFF", actual)
        return True

    _LOGGER.error("[LOG] eco_mode FAILED after retry (expected %s, got %s)", option, actual)
    return False

_LOGGER = logging.getLogger(__name__)


def _log_noise(msg: str, *args):
    """Log a [...] noise message only if log_noise is enabled."""
    if _LOG_NOISE:
        _LOGGER.info("[...] " + msg, *args)


async def _ensure_eco_mode_off() -> bool:
    """Check actual eco_mode state from HA and disable if ON. Returns True if OFF."""
    global _ECO_MODE_ENABLED
    loop = asyncio.get_event_loop()
    actual = await loop.run_in_executor(None, _get_eco_mode_state)
    if actual == "off":
        _ECO_MODE_ENABLED = False
        return True
    if actual == "eco_mode":
        _ECO_MODE_ENABLED = True
        _LOGGER.info("[LOG] eco_mode is ON (actual HA state), disabling...")
        success = await set_eco_mode(False)
        if success:
            return True
        _LOGGER.error("[LOG] eco_mode disable failed")
        return False
    _LOGGER.warning("[LOG] eco_mode state unknown: %s", actual)
    return False


async def _eco_mode_bounce_guard():
    """Monitor eco_mode for 60s after disable. Re-disable if it bounces back."""
    for i in range(6):  # 6 checks x 10s = 60s
        await asyncio.sleep(10)
        if not _charging_enabled:
            _LOGGER.info("[LOG] Bounce guard: charging disabled, stopping monitor")
            return
        loop = asyncio.get_event_loop()
        actual = await loop.run_in_executor(None, _get_eco_mode_state)
        if actual == "eco_mode":
            _LOGGER.warning("[LOG] Bounce guard: eco_mode bounced back to ON at check %d/6, re-disabling", i + 1)
            await set_eco_mode(False)
        elif actual == "off":
            pass  # Good, still off
        else:
            _LOGGER.warning("[LOG] Bounce guard: eco_mode state unknown: %s", actual)
    _LOGGER.info("[LOG] Bounce guard: 60s monitoring complete, eco_mode stable")

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
_charging_enabled: bool = False
_max_current_amps: int = 6
_pending_start_transaction_ids: set = set()

_STATE_FILE = os.getenv("STATE_FILE", "/data/sniffer_state.json")


def _save_state():
    try:
        state = {
            "last_id_tag": _charger_info["last_id_tag"],
            "vendor": _charger_info["vendor"],
            "model": _charger_info["model"],
            "firmware": _charger_info["firmware"],
            "serial": _charger_info["serial"],
            "last_session": _last_session,
            "charging_enabled": _charging_enabled,
            "max_current_amps": _max_current_amps,
        }
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        _LOGGER.exception("[LOG] Failed to save state")


def _load_state():
    global _charging_enabled, _max_current_amps
    try:
        if not os.path.exists(_STATE_FILE):
            return
        with open(_STATE_FILE) as f:
            state = json.load(f)
        _charger_info["last_id_tag"] = state.get("last_id_tag", "")
        _charger_info["vendor"] = state.get("vendor", "unknown")
        _charger_info["model"] = state.get("model", "unknown")
        _charger_info["firmware"] = state.get("firmware", "unknown")
        _charger_info["serial"] = state.get("serial", "unknown")
        _charger_info["connected"] = False
        _last_session.update(state.get("last_session", {}))
        _charging_enabled = state.get("charging_enabled", False)
        _max_current_amps = state.get("max_current_amps", 6)
        _LOGGER.info(
            "[LOG] Restored state: last_id_tag=%s charging_enabled=%s",
            _charger_info["last_id_tag"],
            _charging_enabled,
        )
    except Exception:
        _LOGGER.exception("[LOG] Failed to load state")


@web.middleware
async def log_all_requests(request, handler):
    real_ip = request.headers.get(
        "CF-Connecting-IP",
        request.headers.get("X-Forwarded-For", request.remote),
    )
    is_noise = request.path in ("/meter_values", "/charger_info")
    if is_noise:
        _log_noise("HTTP %s %s from %s WS-Proto=%s UA=%s",
            request.method, request.path_qs, real_ip,
            request.headers.get("Sec-WebSocket-Protocol", ""),
            request.headers.get("User-Agent", ""))
    else:
        _LOGGER.info("[LOG] HTTP %s %s from %s WS-Proto=%s UA=%s",
            request.method, request.path_qs, real_ip,
            request.headers.get("Sec-WebSocket-Protocol", ""),
            request.headers.get("User-Agent", ""))
    return await handler(request)


def _sniff(raw: str) -> str:
    """Sniff OCPP messages. Returns 'start' for StartTransaction, 'charging' for StatusNotification:Charging, '' otherwise."""
    global _charging_enabled
    try:
        msg = json.loads(raw)
        if not isinstance(msg, list) or len(msg) < 3:
            return ""
        msg_type = msg[0]
        action = msg[2] if len(msg) > 2 else ""
        payload = msg[3] if len(msg) > 3 else {}

        if msg_type == 3:
            msg_id = msg[1]
            if msg_id in _pending_responses:
                _pending_responses[msg_id]["response"] = msg
                _pending_responses[msg_id]["event"].set()
            if msg_id in _pending_start_transaction_ids and len(msg) > 2:
                transaction_id = (
                    msg[2].get("transactionId") if isinstance(msg[2], dict) else None
                )
                if transaction_id:
                    _last_session["transaction_id"] = transaction_id
                    _LOGGER.info(
                        "[LOG] Captured transactionId=%s from StartTransaction response",
                        transaction_id,
                    )
                    _save_state()
                _pending_start_transaction_ids.discard(msg_id)
            return ""

        if action in ("Authorize", "StartTransaction"):
            id_tag = payload.get("idTag") or payload.get("id_tag")
            if id_tag:
                _charger_info["last_id_tag"] = id_tag
                _LOGGER.info("[LOG] Captured idTag=%s from %s", id_tag, action)
                _save_state()
            if action == "StartTransaction":
                msg_id = msg[1]
                _pending_start_transaction_ids.add(msg_id)
                _last_session["id_tag"] = id_tag
                _last_session["start_time"] = payload.get("timestamp")
                _last_session["meter_start_wh"] = payload.get("meterStart")
                _last_session["stop_time"] = None
                _last_session["stop_reason"] = None
                _last_session["energy_wh"] = None
                _last_session["transaction_id"] = 0
                _save_state()
                return "start"

        if action == "BootNotification":
            _charger_info["vendor"] = payload.get("chargePointVendor")
            _charger_info["model"] = payload.get("chargePointModel")
            _charger_info["firmware"] = payload.get("firmwareVersion")
            _charger_info["serial"] = payload.get("chargePointSerialNumber")

        if action == "StatusNotification":
            connector_id = payload.get("connectorId", 1)
            ocpp_status = payload.get("status", "")
            if connector_id == 0:
                _log_noise(
                    "StatusNotification connectorId=0 status=%s (charger-level, ignored for evcc)",
                    ocpp_status,
                )
                return ""
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
            if ocpp_status == "Available" and _charging_enabled:
                _charging_enabled = False
                _LOGGER.info("[LOG] Session ended: reset charging_enabled to False")
                _save_state()
                # eco_mode restore is handled by /enable/false handler, not here.
                # StatusNotification "Available" can fire mid-session (e.g. replug)
                # and would prematurely restore eco_mode, blocking the next charge.
            if ocpp_status == "Charging" and not _charging_enabled:
                return "charging"

        if action == "StopTransaction":
            meter_stop = payload.get("meterStop")
            _last_session["stop_time"] = payload.get("timestamp")
            _last_session["meter_stop_wh"] = meter_stop
            _last_session["stop_reason"] = payload.get("reason", "Unknown")
            if _last_session["meter_start_wh"] is not None and meter_stop is not None:
                _last_session["energy_wh"] = (
                    meter_stop - _last_session["meter_start_wh"]
                )
            id_tag = (
                payload.get("idTag")
                or payload.get("id_tag")
                or _last_session.get("id_tag")
            )
            _last_session["id_tag"] = id_tag
            _LOGGER.info(
                "[LOG] StopTransaction: idTag=%s energy=%s Wh reason=%s",
                id_tag,
                _last_session["energy_wh"],
                _last_session["stop_reason"],
            )
            _save_state()

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
            _log_noise("DataTransfer: %s", entry)

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
        return ""
    except Exception:
        return ""


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
            _LOGGER.warning(
                "[LOG] Charger connection rejected: wrong password from %s", request.remote
            )
            return web.Response(status=401, text="Unauthorized")

    ws = web.WebSocketResponse(protocols=("ocpp1.6", "ocpp2.0.1"))
    await ws.prepare(request)

    upstream_url = config.upstream_url or None

    _charger_info["connected"] = True
    _active_charger_ws = ws
    _LOGGER.info("[LOG] Charger connected. Upstream: %s", upstream_url or "none")

    upstream_ws = None
    if upstream_url:
        try:
            upstream_ws = await _connect_upstream(upstream_url)
            _LOGGER.info("[LOG] Connected to upstream %s", upstream_url)
        except Exception:
            _LOGGER.exception("[LOG] Failed to connect to upstream")

    pending_charger_msgs: asyncio.Queue = asyncio.Queue()

    async def throttle_to_zero():
        global _charging_enabled
        await asyncio.sleep(1)
        if _charging_enabled:
            _log_noise(
                "AUTO-THROTTLE: skipped (evcc already enabled at %dA)",
                _max_current_amps,
            )
            return
        _charging_enabled = False
        try:
            _LOGGER.info("[LOG] AUTO-THROTTLE: clearing all charging profiles")
            await _send_to_charger("ClearChargingProfile", {"connectorId": 1})
            _LOGGER.info("[LOG] AUTO-THROTTLE: setting current to 0A")
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
            _LOGGER.info("[LOG] AUTO-THROTTLE: charger set to 0A")
        except Exception:
            _LOGGER.exception("[LOG] AUTO-THROTTLE: failed to set 0A")

    async def charger_to_upstream():
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                break
            try:
                _action = json.loads(msg.data)[2] if len(json.loads(msg.data)) > 2 else ""
            except Exception:
                _action = ""
            if _action in ("MeterValues", "Heartbeat"):
                _log_noise("CHARGER -> UPSTREAM: %s", msg.data)
            else:
                _LOGGER.info("[LOG] CHARGER -> UPSTREAM: %s", msg.data)
            sniff_result = _sniff(msg.data)
            if sniff_result in ("start", "charging") and _auto_throttle:
                asyncio.create_task(throttle_to_zero())
            try:
                parsed = json.loads(msg.data)
                if (
                    isinstance(parsed, list)
                    and len(parsed) >= 2
                    and parsed[0] == 3
                    and parsed[1] in _pending_responses
                ):
                    _log_noise(
                        "FILTERED: not forwarding injected response %s to upstream",
                        parsed[1],
                    )
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
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
                        _log_noise("Reconnecting to upstream %s", upstream_url)
                        upstream_ws = await _connect_upstream(upstream_url)
                        _log_noise("Reconnected to upstream %s", upstream_url)
                        asyncio.create_task(upstream_to_charger_loop(upstream_ws))
                    except Exception:
                        _LOGGER.exception("[LOG] Failed to reconnect to upstream")
                        continue
            try:
                await upstream_ws.send(raw)
            except Exception:
                _LOGGER.exception("[LOG] Failed to send to upstream")

    async def upstream_to_charger_loop(u_ws):
        try:
            async for raw in u_ws:
                try:
                    _is_ack = (json.loads(raw)[0] == 3 and json.loads(raw)[2] == {})
                except Exception:
                    _is_ack = False
                if _is_ack:
                    _log_noise("UPSTREAM -> CHARGER: %s", raw)
                else:
                    _LOGGER.info("[LOG] UPSTREAM -> CHARGER: %s", raw)
                _sniff(raw)
                try:
                    await ws.send_str(raw)
                except Exception:
                    _LOGGER.exception("[LOG] Failed to send to charger")
                    break
        except Exception:
            _log_noise("Upstream connection closed")

    try:
        if upstream_ws:
            asyncio.create_task(upstream_to_charger_loop(upstream_ws))
        await asyncio.gather(charger_to_upstream(), upstream_relay())
    except Exception:
        _LOGGER.exception("[LOG] Proxy error")
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
        _LOGGER.info("[LOG] INJECTED -> CHARGER: %s", msg)
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
    global _charging_enabled, _eco_mode_bounce_task
    enable = request.match_info.get("enable", "true").lower() == "true"
    if not _active_charger_ws:
        return web.json_response({"error": "no charger connected"}, status=503)

    # --- ENABLE ---
    if enable:
        # Change 2: Never skip /enable/true. Always check eco_mode and re-send profile.
        # Even if _charging_enabled is already True, eco_mode may have bounced back.
        was_already_enabled = _charging_enabled

        # Change 1: Synchronous eco_mode disable. Do NOT send profile until eco_mode is OFF.
        if _ECO_MODE_MANAGEMENT:
            # Change 4: Read actual HA state, not in-memory flag
            eco_off = await _ensure_eco_mode_off()
            if not eco_off:
                _LOGGER.error("[LOG] enable=True: eco_mode could not be disabled, aborting")
                return web.json_response(
                    {"error": "eco_mode disable failed", "enable": True, "sent": False}, status=503
                )

        _charging_enabled = True
        _save_state()
        limit = _max_current_amps
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
        try:
            response = await _send_to_charger("SetChargingProfile", payload)
            if was_already_enabled:
                _LOGGER.info("[LOG] enable=True: re-sent SetChargingProfile %dA (was already enabled, re-verified eco_mode)", limit)
            else:
                _LOGGER.info("[LOG] enable=True: sent SetChargingProfile %dA", limit)

            # Change 3: Start bounce guard in background
            if _ECO_MODE_MANAGEMENT:
                if _eco_mode_bounce_task and not _eco_mode_bounce_task.done():
                    _eco_mode_bounce_task.cancel()
                _eco_mode_bounce_task = asyncio.create_task(_eco_mode_bounce_guard())

            return web.json_response(
                {
                    "action": "SetChargingProfile",
                    "enable": True,
                    "limit_amps": limit,
                    "sent": True,
                    "was_already_enabled": was_already_enabled,
                    "response": response,
                }
            )
        except asyncio.TimeoutError:
            return web.json_response({"error": "charger did not respond"}, status=504)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # --- DISABLE ---
    # Cancel bounce guard if running
    if _eco_mode_bounce_task and not _eco_mode_bounce_task.done():
        _eco_mode_bounce_task.cancel()
        _eco_mode_bounce_task = None

    if not _charging_enabled:
        _LOGGER.info("[LOG] enable=False: already disabled, skipping")
        return web.json_response(
            {"action": "SetChargingProfile", "enable": False, "sent": False, "reason": "already_disabled"}
        )

    # Re-enable eco_mode when EVCC disables charging.
    # Delay 10s so the 0A profile reaches the Wallbox first.
    if _ECO_MODE_MANAGEMENT:
        async def _delayed_eco_restore_on_disable():
            await asyncio.sleep(10)
            if not _charging_enabled:
                _LOGGER.info("[LOG] Re-enabling eco_mode after EVCC disabled charging")
                await set_eco_mode(True)
            else:
                _LOGGER.info("[LOG] Skipping eco_mode restore: charging re-enabled")
        asyncio.create_task(_delayed_eco_restore_on_disable())

    try:
        _charging_enabled = False
        _save_state()
        payload = {
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
        }
        response = await _send_to_charger("SetChargingProfile", payload)
        return web.json_response(
            {"action": "SetChargingProfile", "enable": False, "limit_amps": 0, "sent": True, "response": response}
        )
    except asyncio.TimeoutError:
        return web.json_response({"error": "charger did not respond"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def maxcurrent_handler(request: web.Request) -> web.Response:
    global _max_current_amps
    try:
        amps = int(request.match_info["amps"])
    except (KeyError, ValueError):
        return web.json_response({"error": "amps required"}, status=400)
    _max_current_amps = amps
    _save_state()
    if not _active_charger_ws:
        return web.json_response({"error": "no charger connected"}, status=503)
    if not _charging_enabled:
        _LOGGER.info("[LOG] maxcurrent=%dA stored but not sent (charging paused)", amps)
        return web.json_response(
            {
                "action": "SetChargingProfile",
                "amps": amps,
                "sent": False,
                "reason": "paused",
            }
        )
    try:
        payload = {
            "connectorId": 1,
            "csChargingProfiles": {
                "chargingProfileId": 2,
                "stackLevel": 1,
                "chargingProfilePurpose": "TxProfile",
                "chargingProfileKind": "Absolute",
                "chargingSchedule": {
                    "chargingRateUnit": "A",
                    "chargingSchedulePeriod": [{"startPeriod": 0, "limit": amps}],
                },
            },
        }
        response = await _send_to_charger("SetChargingProfile", payload)
        return web.json_response(
            {
                "action": "SetChargingProfile",
                "amps": amps,
                "sent": True,
                "response": response,
            }
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
    writer.writerow(
        ["timestamp", "backend_id", "duration_s", "energy_kwh", "revenue", "id_tag"]
    )
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
  <li>POST /remote_start/{id_tag} - RemoteStartTransaction with given RFID tag</li>
  <li>POST /remote_stop - RemoteStopTransaction for current session</li>
  <li>POST /remote_restart/{id_tag} - stop current session + start new one with given RFID tag</li>
</ul>
</body></html>"""
    return web.Response(text=html, content_type="text/html")


async def remote_start_handler(request: web.Request) -> web.Response:
    id_tag = request.match_info.get("id_tag", "")
    if not id_tag:
        return web.json_response({"error": "id_tag required"}, status=400)
    if not _active_charger_ws:
        return web.json_response({"error": "no charger connected"}, status=503)
    try:
        payload = {"connectorId": 1, "idTag": id_tag}
        response = await _send_to_charger("RemoteStartTransaction", payload)
        _LOGGER.info("[LOG] RemoteStartTransaction: idTag=%s response=%s", id_tag, response)
        return web.json_response(
            {"action": "RemoteStartTransaction", "id_tag": id_tag, "response": response}
        )
    except asyncio.TimeoutError:
        return web.json_response({"error": "charger did not respond"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def remote_stop_handler(request: web.Request) -> web.Response:
    if not _active_charger_ws:
        return web.json_response({"error": "no charger connected"}, status=503)
    txn_id = _last_session.get("transaction_id", 0)
    if not txn_id:
        return web.json_response({"error": "no active transaction"}, status=400)
    try:
        response = await _send_to_charger(
            "RemoteStopTransaction", {"transactionId": txn_id}
        )
        _LOGGER.info("[LOG] RemoteStopTransaction: txn=%s response=%s", txn_id, response)
        return web.json_response(
            {
                "action": "RemoteStopTransaction",
                "transaction_id": txn_id,
                "response": response,
            }
        )
    except asyncio.TimeoutError:
        return web.json_response({"error": "charger did not respond"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def remote_restart_handler(request: web.Request) -> web.Response:
    id_tag = request.match_info.get("id_tag", "")
    if not id_tag:
        return web.json_response({"error": "id_tag required"}, status=400)
    if not _active_charger_ws:
        return web.json_response({"error": "no charger connected"}, status=503)
    results = {}
    txn_id = _last_session.get("transaction_id", 0)
    if txn_id:
        try:
            stop_resp = await _send_to_charger(
                "RemoteStopTransaction", {"transactionId": txn_id}
            )
            results["stop"] = {"transaction_id": txn_id, "response": stop_resp}
            _LOGGER.info("[LOG] RemoteStopTransaction: txn=%s response=%s", txn_id, stop_resp)
            await asyncio.sleep(2)
        except asyncio.TimeoutError:
            results["stop"] = {"error": "charger did not respond"}
        except Exception as e:
            results["stop"] = {"error": str(e)}
    try:
        start_resp = await _send_to_charger(
            "RemoteStartTransaction", {"connectorId": 1, "idTag": id_tag}
        )
        results["start"] = {"id_tag": id_tag, "response": start_resp}
        _LOGGER.info("[LOG] RemoteStartTransaction: idTag=%s response=%s", id_tag, start_resp)
    except asyncio.TimeoutError:
        results["start"] = {"error": "charger did not respond"}
    except Exception as e:
        results["start"] = {"error": str(e)}
    return web.json_response({"action": "RemoteRestart", "results": results})


async def init_app() -> web.Application:
    global _auto_throttle, _min_current, _ECO_MODE_ENTITY, _ECO_MODE_MANAGEMENT, _LOG_NOISE
    config = Config()
    _auto_throttle = config.auto_throttle
    _min_current = config.min_current
    _ECO_MODE_ENTITY = config.eco_mode_entity
    _ECO_MODE_MANAGEMENT = config.eco_mode_management
    _LOG_NOISE = config.log_noise
    _load_state()
    if _auto_throttle:
        _LOGGER.info(
            "[LOG] Auto-throttle enabled: charger set to 0A on StartTransaction, evcc controls via /enable"
        )
    if _ECO_MODE_MANAGEMENT and _ECO_MODE_ENTITY:
        # Sync _ECO_MODE_ENABLED with actual HA entity state on startup
        actual = _get_eco_mode_state()
        if actual != "unknown":
            _ECO_MODE_ENABLED = (actual == "eco_mode")
            _LOGGER.info(
                "[LOG] eco_mode management enabled: entity=%s, current state=%s, tracked=%s",
                _ECO_MODE_ENTITY, actual, "ON" if _ECO_MODE_ENABLED else "OFF"
            )
        else:
            _LOGGER.info(
                "[LOG] eco_mode management enabled: entity=%s (could not read initial state)",
                _ECO_MODE_ENTITY
            )
    if config.charger_password:
        _LOGGER.info(
            "[LOG] Charger password configured: only authenticated chargers accepted"
        )
    else:
        _LOGGER.warning("[LOG] No charger password set: any charger can connect")

    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
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
            web.post("/remote_start/{id_tag}", remote_start_handler),
            web.post("/remote_stop", remote_stop_handler),
            web.post("/remote_restart/{id_tag}", remote_restart_handler),
        ]
    )
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = asyncio.run(init_app())
    web.run_app(app, port=int(os.getenv("PORT", 9000)))


if __name__ == "__main__":
    main()
