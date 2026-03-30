import json
import pytest
from ocpp_proxy.main import _sniff, _charger_info, _meter_values, _last_session, _data_transfer_log


def _reset():
    _charger_info.update({
        "connected": False, "vendor": "unknown", "model": "unknown",
        "last_id_tag": "", "last_status": "Available", "evcc_status": "A",
        "firmware": "unknown", "serial": "unknown",
    })
    _meter_values.update({
        "energy_wh": 0.0, "power_w": 0.0,
        "current_l1": 0.0, "current_l2": 0.0, "current_l3": 0.0,
        "voltage_l1": 0.0, "voltage_l2": 0.0, "voltage_l3": 0.0,
        "timestamp": None,
    })
    _last_session.update({
        "id_tag": "", "transaction_id": 0, "start_time": "", "stop_time": "",
        "meter_start_wh": 0.0, "meter_stop_wh": 0.0, "energy_wh": 0.0, "stop_reason": "",
    })
    _data_transfer_log.clear()


class TestSniffAuthorize:
    def setup_method(self):
        _reset()

    def test_authorize_captures_idtag(self):
        msg = json.dumps([2, "1", "Authorize", {"idTag": "97BA7F51"}])
        result = _sniff(msg)
        assert _charger_info["last_id_tag"] == "97BA7F51"
        assert result is False

    def test_authorize_with_id_tag_key(self):
        msg = json.dumps([2, "1", "Authorize", {"id_tag": "AABBCCDD"}])
        result = _sniff(msg)
        assert _charger_info["last_id_tag"] == "AABBCCDD"


class TestSniffStartTransaction:
    def setup_method(self):
        _reset()

    def test_start_transaction_captures_idtag(self):
        msg = json.dumps([2, "1", "StartTransaction", {
            "connectorId": 1, "idTag": "97BA7F51",
            "meterStart": 111000, "timestamp": "2026-03-30T08:00:00Z"
        }])
        result = _sniff(msg)
        assert result is True
        assert _charger_info["last_id_tag"] == "97BA7F51"
        assert _last_session["id_tag"] == "97BA7F51"
        assert _last_session["meter_start_wh"] == 111000
        assert _last_session["start_time"] == "2026-03-30T08:00:00Z"

    def test_start_transaction_returns_true_for_auto_throttle(self):
        msg = json.dumps([2, "1", "StartTransaction", {
            "connectorId": 1, "idTag": "AABB", "meterStart": 0,
            "timestamp": "2026-01-01T00:00:00Z"
        }])
        assert _sniff(msg) is True


class TestSniffStopTransaction:
    def setup_method(self):
        _reset()

    def test_stop_transaction_calculates_energy(self):
        start = json.dumps([2, "1", "StartTransaction", {
            "connectorId": 1, "idTag": "97BA7F51",
            "meterStart": 111000, "timestamp": "2026-03-30T08:00:00Z"
        }])
        _sniff(start)

        stop = json.dumps([2, "2", "StopTransaction", {
            "meterStop": 118400, "timestamp": "2026-03-30T10:30:00Z",
            "reason": "Local"
        }])
        _sniff(stop)

        assert _last_session["energy_wh"] == 7400
        assert _last_session["stop_reason"] == "Local"
        assert _last_session["meter_stop_wh"] == 118400
        assert _last_session["id_tag"] == "97BA7F51"

    def test_stop_transaction_preserves_idtag_from_start(self):
        start = json.dumps([2, "1", "StartTransaction", {
            "connectorId": 1, "idTag": "DEADBEEF",
            "meterStart": 0, "timestamp": "2026-01-01T00:00:00Z"
        }])
        _sniff(start)

        stop = json.dumps([2, "2", "StopTransaction", {
            "meterStop": 5000, "timestamp": "2026-01-01T01:00:00Z"
        }])
        _sniff(stop)
        assert _last_session["id_tag"] == "DEADBEEF"


class TestSniffBootNotification:
    def setup_method(self):
        _reset()

    def test_boot_notification_captures_charger_info(self):
        msg = json.dumps([2, "1", "BootNotification", {
            "chargePointVendor": "Wall Box Chargers",
            "chargePointModel": "PPR1-0-2-4",
            "firmwareVersion": "6.11.16",
            "chargePointSerialNumber": "1305884"
        }])
        _sniff(msg)
        assert _charger_info["vendor"] == "Wall Box Chargers"
        assert _charger_info["model"] == "PPR1-0-2-4"
        assert _charger_info["firmware"] == "6.11.16"
        assert _charger_info["serial"] == "1305884"


class TestSniffStatusNotification:
    def setup_method(self):
        _reset()

    def test_available_maps_to_a(self):
        msg = json.dumps([2, "1", "StatusNotification", {
            "connectorId": 1, "errorCode": "NoError", "status": "Available"
        }])
        _sniff(msg)
        assert _charger_info["evcc_status"] == "A"
        assert _charger_info["last_status"] == "Available"

    def test_preparing_maps_to_b(self):
        msg = json.dumps([2, "1", "StatusNotification", {
            "connectorId": 1, "errorCode": "NoError", "status": "Preparing"
        }])
        _sniff(msg)
        assert _charger_info["evcc_status"] == "B"

    def test_charging_maps_to_c(self):
        msg = json.dumps([2, "1", "StatusNotification", {
            "connectorId": 1, "errorCode": "NoError", "status": "Charging"
        }])
        _sniff(msg)
        assert _charger_info["evcc_status"] == "C"

    def test_suspended_ev_maps_to_b(self):
        msg = json.dumps([2, "1", "StatusNotification", {
            "connectorId": 1, "errorCode": "NoError", "status": "SuspendedEV"
        }])
        _sniff(msg)
        assert _charger_info["evcc_status"] == "B"

    def test_suspended_evse_maps_to_b(self):
        msg = json.dumps([2, "1", "StatusNotification", {
            "connectorId": 1, "errorCode": "NoError", "status": "SuspendedEVSE"
        }])
        _sniff(msg)
        assert _charger_info["evcc_status"] == "B"

    def test_faulted_maps_to_f(self):
        msg = json.dumps([2, "1", "StatusNotification", {
            "connectorId": 1, "errorCode": "InternalError", "status": "Faulted"
        }])
        _sniff(msg)
        assert _charger_info["evcc_status"] == "F"

    def test_unknown_status_defaults_to_a(self):
        msg = json.dumps([2, "1", "StatusNotification", {
            "connectorId": 1, "errorCode": "NoError", "status": "WeirdStatus"
        }])
        _sniff(msg)
        assert _charger_info["evcc_status"] == "A"


class TestSniffMeterValues:
    def setup_method(self):
        _reset()

    def test_meter_values_parses_all_phases(self):
        msg = json.dumps([2, "1", "MeterValues", {
            "connectorId": 1,
            "meterValue": [{"timestamp": "2026-03-30T14:00:00Z", "sampledValue": [
                {"measurand": "Energy.Active.Import.Register", "unit": "Wh", "value": "111335.0"},
                {"measurand": "Power.Active.Import", "unit": "W", "value": "7400.0"},
                {"measurand": "Current.Import", "phase": "L1", "unit": "A", "value": "10.5"},
                {"measurand": "Current.Import", "phase": "L2", "unit": "A", "value": "10.5"},
                {"measurand": "Current.Import", "phase": "L3", "unit": "A", "value": "10.4"},
                {"measurand": "Voltage", "phase": "L1-N", "unit": "V", "value": "235.0"},
                {"measurand": "Voltage", "phase": "L2-N", "unit": "V", "value": "229.0"},
                {"measurand": "Voltage", "phase": "L3-N", "unit": "V", "value": "230.0"},
            ]}]
        }])
        _sniff(msg)
        assert _meter_values["energy_wh"] == 111335.0
        assert _meter_values["power_w"] == 7400.0
        assert _meter_values["current_l1"] == 10.5
        assert _meter_values["current_l2"] == 10.5
        assert _meter_values["current_l3"] == 10.4
        assert _meter_values["voltage_l1"] == 235.0
        assert _meter_values["voltage_l2"] == 229.0
        assert _meter_values["voltage_l3"] == 230.0
        assert _meter_values["timestamp"] == "2026-03-30T14:00:00Z"

    def test_meter_values_skips_none_values(self):
        msg = json.dumps([2, "1", "MeterValues", {
            "connectorId": 1,
            "meterValue": [{"timestamp": "2026-03-30T14:00:00Z", "sampledValue": [
                {"measurand": "Power.Active.Import", "unit": "W", "value": None},
            ]}]
        }])
        _sniff(msg)
        assert _meter_values["power_w"] == 0.0


class TestSniffDataTransfer:
    def setup_method(self):
        _reset()

    def test_data_transfer_logged(self):
        msg = json.dumps([2, "1", "DataTransfer", {
            "vendorId": "com.wallbox", "messageId": "test", "data": "payload"
        }])
        _sniff(msg)
        assert len(_data_transfer_log) == 1
        assert _data_transfer_log[0]["vendorId"] == "com.wallbox"

    def test_data_transfer_max_100(self):
        for i in range(110):
            msg = json.dumps([2, str(i), "DataTransfer", {
                "vendorId": "v", "messageId": str(i), "data": ""
            }])
            _sniff(msg)
        assert len(_data_transfer_log) == 100


class TestSniffResponseCorrelation:
    def setup_method(self):
        _reset()

    def test_response_does_not_trigger_start(self):
        msg = json.dumps([3, "1", {"status": "Accepted"}])
        result = _sniff(msg)
        assert result is False

    def test_invalid_json_returns_false(self):
        assert _sniff("not json") is False

    def test_empty_array_returns_false(self):
        assert _sniff("[]") is False

    def test_short_array_returns_false(self):
        assert _sniff("[1]") is False


class TestSniffDefaults:
    def setup_method(self):
        _reset()

    def test_charger_info_defaults(self):
        assert _charger_info["connected"] is False
        assert _charger_info["last_id_tag"] == ""
        assert _charger_info["evcc_status"] == "A"
        assert _charger_info["vendor"] == "unknown"

    def test_meter_values_defaults_to_zero(self):
        assert _meter_values["power_w"] == 0.0
        assert _meter_values["current_l1"] == 0.0
        assert _meter_values["voltage_l1"] == 0.0

    def test_last_session_defaults_to_zero(self):
        assert _last_session["energy_wh"] == 0.0
        assert _last_session["id_tag"] == ""
        assert _last_session["transaction_id"] == 0
