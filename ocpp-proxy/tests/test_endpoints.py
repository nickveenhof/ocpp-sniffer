import json
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from ocpp_proxy.main import init_app, _charger_info, _meter_values, _last_session, _active_charger_ws
import ocpp_proxy.main as main_module


class TestReadEndpoints(AioHTTPTestCase):
    async def get_application(self):
        return await init_app()

    @unittest_run_loop
    async def test_welcome_page(self):
        resp = await self.client.request("GET", "/")
        assert resp.status == 200
        text = await resp.text()
        assert "OCPP Sniffer" in text

    @unittest_run_loop
    async def test_charger_info_returns_defaults(self):
        resp = await self.client.request("GET", "/charger_info")
        assert resp.status == 200
        data = await resp.json()
        assert data["connected"] is False
        assert data["evcc_status"] == "A"
        assert data["last_id_tag"] == ""

    @unittest_run_loop
    async def test_meter_values_returns_zeros(self):
        resp = await self.client.request("GET", "/meter_values")
        assert resp.status == 200
        data = await resp.json()
        assert data["power_w"] == 0.0
        assert data["current_l1"] == 0.0
        assert data["voltage_l1"] == 0.0

    @unittest_run_loop
    async def test_last_session_returns_defaults(self):
        resp = await self.client.request("GET", "/last_session")
        assert resp.status == 200
        data = await resp.json()
        assert data["energy_wh"] == 0.0
        assert data["id_tag"] == ""

    @unittest_run_loop
    async def test_status_returns_upstream(self):
        resp = await self.client.request("GET", "/status")
        assert resp.status == 200
        data = await resp.json()
        assert "charger_connected" in data
        assert "upstream" in data

    @unittest_run_loop
    async def test_data_transfer_returns_empty(self):
        resp = await self.client.request("GET", "/data_transfer")
        assert resp.status == 200
        data = await resp.json()
        assert data == []

    @unittest_run_loop
    async def test_sessions_returns_empty(self):
        resp = await self.client.request("GET", "/sessions")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)

    @unittest_run_loop
    async def test_sessions_csv_returns_header(self):
        resp = await self.client.request("GET", "/sessions.csv")
        assert resp.status == 200
        text = await resp.text()
        assert "timestamp" in text
        assert "id_tag" in text


class TestCommandEndpointsNoCharger(AioHTTPTestCase):
    async def get_application(self):
        return await init_app()

    @unittest_run_loop
    async def test_enable_without_charger_returns_503(self):
        main_module._active_charger_ws = None
        resp = await self.client.request("POST", "/enable/true")
        assert resp.status == 503

    @unittest_run_loop
    async def test_maxcurrent_without_charger_returns_503(self):
        main_module._active_charger_ws = None
        resp = await self.client.request("POST", "/maxcurrent/16")
        assert resp.status == 503

    @unittest_run_loop
    async def test_command_without_charger_returns_503(self):
        main_module._active_charger_ws = None
        resp = await self.client.request("POST", "/command",
            json={"action": "GetConfiguration", "payload": {}})
        assert resp.status == 503

    @unittest_run_loop
    async def test_command_without_action_returns_400(self):
        resp = await self.client.request("POST", "/command", json={"payload": {}})
        assert resp.status == 400

    @unittest_run_loop
    async def test_command_invalid_json_returns_400(self):
        resp = await self.client.request("POST", "/command", data="not json",
            headers={"Content-Type": "application/json"})
        assert resp.status == 400
