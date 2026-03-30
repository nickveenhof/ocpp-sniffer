# OCPP Sniffer - Transparent OCPP Proxy for RFID Identification

A minimal, transparent OCPP 1.6 proxy that sits between your EV charger and your CPO backend (e.g. Wattify). It forwards all traffic unchanged and captures RFID idTags for use in EVCC vehicle identification.

## What it does

```
Wallbox ──wss──► OCPP Sniffer ──wss──► Wattify (CPO)
                     │
                     └──► /charger_info  (idTag for EVCC)
```

1. Wallbox connects to the proxy instead of directly to Wattify.
2. Every OCPP message is forwarded to Wattify unchanged.
3. Every Wattify response is relayed back to the charger unchanged.
4. Wattify remains in full control of authorization and billing.
5. The proxy sniffs `Authorize` and `StartTransaction` messages and records the `idTag`.
6. EVCC polls `/charger_info` to read the last `idTag` and identify the vehicle.

## Installation

### Home Assistant Add-on

1. In HA: **Settings > Add-ons > Add-on Store > Repositories**
2. Add: `https://github.com/nickveenhof/ocpp-proxy`
3. Install **OCPP Sniffer**
4. Configure (see below)
5. Start

## Configuration

```yaml
ocpp_services:
  - id: "wattify"
    url: "wss://cpo.wattify.be/ocpp/YOUR_SERIAL"
    auth_type: "none"
    enabled: true
```

All other settings are optional and unused in transparent proxy mode.

## Charger setup

Point your charger's OCPP URL to the proxy instead of your CPO:

| Setting | Value |
|---|---|
| OCPP URL | `wss://ocpp.yourdomain.com/charger` |
| Identity | your charger serial number |
| Password | (empty) |

## REST API

| Endpoint | Description |
|---|---|
| `GET /charger_info` | Last captured idTag and charger state. Poll this from EVCC. |
| `GET /status` | Upstream URL and charger connection state |
| `GET /sessions` | Completed sessions with idTag (JSON) |
| `GET /sessions.csv` | Completed sessions (CSV) |

### `/charger_info` response example

```json
{
  "connected": true,
  "vendor": "Wall Box Chargers",
  "model": "PPR1-0-2-4",
  "last_id_tag": "97BA7F51",
  "last_status": "Preparing"
}
```

## EVCC integration

In `evcc.yaml`, use `type: custom` and poll `/charger_info` for the idTag:

```yaml
chargers:
  - name: wallbox
    type: custom
    status:
      source: homeassistant
      entity: sensor.wallbox_evcc_status
    enabled:
      source: homeassistant
      entity: sensor.wallbox_evcc_enabled
    enable:
      source: homeassistant
      entity: switch.wallbox_pulsar_pro_sn_1305884_pause_resume
    maxcurrent:
      source: homeassistant
      entity: number.wallbox_pulsar_pro_sn_1305884_maximum_charging_current
    power:
      source: homeassistant
      entity: sensor.wallbox_charge_power_w
    energy:
      source: homeassistant
      entity: sensor.wallbox_pulsar_pro_sn_1305884_added_energy
    identify:
      source: http
      uri: http://192.168.1.126:9000/charger_info
      jq: .last_id_tag

vehicles:
  - name: polestar4
    type: polestar
    identifiers:
      - 97BA7F51
```

## Architecture

This proxy does NOT act as a Central System. Wattify remains the Central System and handles all authorization and billing. The proxy is transparent to both sides.

The only thing the proxy does beyond forwarding is:
- Log `idTag` from `Authorize` and `StartTransaction` messages
- Expose `idTag` via REST for EVCC vehicle identification

## License

MIT
