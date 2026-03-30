import json
import logging
import os

_LOGGER = logging.getLogger(__name__)


class Config:
    def __init__(self, path: str | None = None):
        default_path = os.getenv("ADDON_CONFIG_FILE", "/data/options.yaml")
        config_path = path or default_path
        json_path = config_path.replace(".yaml", ".json")
        self._cfg: dict = {}
        if os.path.exists(config_path):
            import yaml

            with open(config_path) as f:
                self._cfg = yaml.safe_load(f) or {}
            _LOGGER.info("Loaded config from %s", config_path)
        elif os.path.exists(json_path):
            with open(json_path) as f:
                self._cfg = json.load(f)
            _LOGGER.info("Loaded config from %s", json_path)
        else:
            _LOGGER.warning("No config file found at %s or %s", config_path, json_path)
        _LOGGER.info("Upstream URL: %s", self.upstream_url or "(none)")

    @property
    def upstream_url(self) -> str:
        return str(self._cfg.get("upstream_url", ""))

    @property
    def ocpp_services(self) -> list[dict]:
        url = self.upstream_url
        if url:
            return [{"id": "upstream", "url": url}]
        return []
