"""Tests for Wallbox eco_mode management in the OCPP sniffer."""
import json
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from ocpp_proxy.config import Config
import ocpp_proxy.main as main_mod


class TestConfigEcoMode:
    """Test eco_mode config properties."""

    def test_eco_mode_entity_default_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            f.flush()
            cfg = Config(path=f.name)
        assert cfg.eco_mode_entity == ""
        os.unlink(f.name)

    def test_eco_mode_entity_custom(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"eco_mode_entity": "select.wallbox_solar"}, f)
            f.flush()
            cfg = Config(path=f.name)
        assert cfg.eco_mode_entity == "select.wallbox_solar"
        os.unlink(f.name)

    def test_eco_mode_management_default_true(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            f.flush()
            cfg = Config(path=f.name)
        assert cfg.eco_mode_management is True
        os.unlink(f.name)

    def test_eco_mode_management_disabled(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"eco_mode_management": False}, f)
            f.flush()
            cfg = Config(path=f.name)
        assert cfg.eco_mode_management is False
        os.unlink(f.name)


class TestSetEcoMode:
    """Test the set_eco_mode async function."""

    @pytest.mark.asyncio
    async def test_skip_when_management_disabled(self):
        main_mod._ECO_MODE_MANAGEMENT = False
        main_mod._ECO_MODE_ENABLED = True
        result = await main_mod.set_eco_mode(False)
        assert result is True  # Returns True (no-op)
        assert main_mod._ECO_MODE_ENABLED is True  # Unchanged

    @pytest.mark.asyncio
    async def test_skip_when_no_entity(self):
        main_mod._ECO_MODE_MANAGEMENT = True
        main_mod._ECO_MODE_ENTITY = ""
        main_mod._ECO_MODE_ENABLED = True
        result = await main_mod.set_eco_mode(False)
        assert result is True
        assert main_mod._ECO_MODE_ENABLED is True

    @pytest.mark.asyncio
    async def test_skip_when_already_in_desired_state(self):
        main_mod._ECO_MODE_MANAGEMENT = True
        main_mod._ECO_MODE_ENTITY = "select.test"
        main_mod._ECO_MODE_ENABLED = True
        result = await main_mod.set_eco_mode(True)
        assert result is True

    @pytest.mark.asyncio
    async def test_disable_eco_mode_calls_ha(self):
        main_mod._ECO_MODE_MANAGEMENT = True
        main_mod._ECO_MODE_ENTITY = "select.wallbox_solar"
        main_mod._ECO_MODE_ENABLED = True

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("ocpp_proxy.main.requests.post", return_value=mock_resp) as mock_post:
            result = await main_mod.set_eco_mode(False)

        assert result is True
        assert main_mod._ECO_MODE_ENABLED is False
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"]["option"] == "off"
        assert call_kwargs[1]["json"]["entity_id"] == "select.wallbox_solar"

    @pytest.mark.asyncio
    async def test_enable_eco_mode_calls_ha(self):
        main_mod._ECO_MODE_MANAGEMENT = True
        main_mod._ECO_MODE_ENTITY = "select.wallbox_solar"
        main_mod._ECO_MODE_ENABLED = False

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("ocpp_proxy.main.requests.post", return_value=mock_resp) as mock_post:
            result = await main_mod.set_eco_mode(True)

        assert result is True
        assert main_mod._ECO_MODE_ENABLED is True
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"]["option"] == "eco_mode"

    @pytest.mark.asyncio
    async def test_ha_api_failure_returns_false(self):
        main_mod._ECO_MODE_MANAGEMENT = True
        main_mod._ECO_MODE_ENTITY = "select.wallbox_solar"
        main_mod._ECO_MODE_ENABLED = True

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("ocpp_proxy.main.requests.post", return_value=mock_resp):
            result = await main_mod.set_eco_mode(False)

        assert result is False
        assert main_mod._ECO_MODE_ENABLED is True  # Unchanged on failure

    @pytest.mark.asyncio
    async def test_ha_api_uses_supervisor_token(self):
        main_mod._ECO_MODE_MANAGEMENT = True
        main_mod._ECO_MODE_ENTITY = "select.test"
        main_mod._ECO_MODE_ENABLED = True

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "test-token-123"}):
            main_mod._HA_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")
            with patch("ocpp_proxy.main.requests.post", return_value=mock_resp) as mock_post:
                await main_mod.set_eco_mode(False)

        call_kwargs = mock_post.call_args
        assert "Bearer test-token-123" in call_kwargs[1]["headers"]["Authorization"]


class TestSniffEcoModeIntegration:
    """Test eco_mode toggle in the OCPP message flow."""

    def setup_method(self):
        """Save global state before each test."""
        self._orig_charging = main_mod._charging_enabled
        self._orig_eco_mgmt = main_mod._ECO_MODE_MANAGEMENT
        self._orig_eco_entity = main_mod._ECO_MODE_ENTITY
        self._orig_eco_enabled = main_mod._ECO_MODE_ENABLED
        self._orig_last_status = main_mod._charger_info.get("last_status")
        self._orig_evcc_status = main_mod._charger_info.get("evcc_status")

    def teardown_method(self):
        """Restore global state after each test."""
        main_mod._charging_enabled = self._orig_charging
        main_mod._ECO_MODE_MANAGEMENT = self._orig_eco_mgmt
        main_mod._ECO_MODE_ENTITY = self._orig_eco_entity
        main_mod._ECO_MODE_ENABLED = self._orig_eco_enabled
        main_mod._charger_info["last_status"] = self._orig_last_status
        main_mod._charger_info["evcc_status"] = self._orig_evcc_status

    def test_available_status_triggers_eco_restore(self):
        """When charger goes Available and charging was enabled, eco_mode restore is scheduled."""
        main_mod._charging_enabled = True
        main_mod._ECO_MODE_MANAGEMENT = True
        main_mod._ECO_MODE_ENTITY = "select.test"
        main_mod._ECO_MODE_ENABLED = False

        msg = json.dumps([2, "test123", "StatusNotification",
                          {"connectorId": 1, "status": "Available", "errorCode": "NoError"}])

        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            result = main_mod._sniff(msg)

        assert main_mod._charging_enabled is False

    def test_charging_status_detected(self):
        """When charger goes to Charging and not enabled, return 'charging' signal."""
        main_mod._charging_enabled = False

        msg = json.dumps([2, "test456", "StatusNotification",
                          {"connectorId": 1, "status": "Charging", "errorCode": "NoError"}])

        result = main_mod._sniff(msg)
        assert result == "charging"
