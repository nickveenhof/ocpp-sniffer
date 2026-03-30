import json
import os
import tempfile
import pytest
from ocpp_proxy.config import Config


class TestConfigFromJson:
    def test_loads_upstream_url(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"upstream_url": "wss://cpo.example.com/ocpp/123"}, f)
            f.flush()
            cfg = Config(path=f.name)
        assert cfg.upstream_url == "wss://cpo.example.com/ocpp/123"
        os.unlink(f.name)

    def test_loads_charger_password(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"charger_password": "secret123"}, f)
            f.flush()
            cfg = Config(path=f.name)
        assert cfg.charger_password == "secret123"
        os.unlink(f.name)

    def test_min_current_default(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            f.flush()
            cfg = Config(path=f.name)
        assert cfg.min_current == 6
        os.unlink(f.name)

    def test_min_current_custom(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"min_current": 8}, f)
            f.flush()
            cfg = Config(path=f.name)
        assert cfg.min_current == 8
        os.unlink(f.name)

    def test_auto_throttle_default_true(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            f.flush()
            cfg = Config(path=f.name)
        assert cfg.auto_throttle is True
        os.unlink(f.name)

    def test_auto_throttle_disabled(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"auto_throttle": False}, f)
            f.flush()
            cfg = Config(path=f.name)
        assert cfg.auto_throttle is False
        os.unlink(f.name)


class TestConfigFallback:
    def test_yaml_fallback_to_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"upstream_url": "wss://test.com"}, f)
            f.flush()
            yaml_path = f.name.replace(".json", ".yaml")
            cfg = Config(path=yaml_path)
        assert cfg.upstream_url == "wss://test.com"
        os.unlink(f.name)

    def test_missing_file_returns_defaults(self):
        cfg = Config(path="/nonexistent/path.yaml")
        assert cfg.upstream_url == ""
        assert cfg.charger_password == ""
        assert cfg.min_current == 6
        assert cfg.auto_throttle is True
