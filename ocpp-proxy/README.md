# OCPP Sniffer

Transparent OCPP 1.6 proxy for Home Assistant. Sits between your charger and your CPO. Forwards all traffic unchanged. Captures RFID tags and meter data for evcc. Optionally controls charging via direct OCPP commands.

## The problem

You use evcc for solar charging. Your charger is managed by a CPO for billing via OCPP. evcc cannot see who plugged in (RFID tag), so it cannot select the right vehicle or schedule.

## The solution

```
Charger ──wss──► OCPP Sniffer ──wss──► CPO  (billing unchanged)
                      │
                      ├── /charger_info   RFID tag, status
                      ├── /meter_values   power, energy, L1/L2/L3
                      ├── /enable/{bool}  pause/resume via SetChargingProfile
                      └── /maxcurrent/N   set max current via SetChargingProfile
evcc ──HTTP──► OCPP Sniffer
```

The CPO stays in full control of authorization and billing. The sniffer reads OCPP traffic and injects SetChargingProfile commands for evcc control.

## What gets captured

| OCPP message | Captured | Endpoint |
|---|---|---|
| `BootNotification` | Vendor, model, firmware, serial | `/charger_info` |
| `StatusNotification` | Status (A/B/C for evcc) | `/charger_info` |
| `Authorize` | RFID idTag | `/charger_info` |
| `StartTransaction` | RFID idTag, meter start | `/charger_info`, `/last_session` |
| `StopTransaction` | Meter stop, energy, stop reason | `/last_session` |
| `MeterValues` | L1/L2/L3 voltage, current, power, energy | `/meter_values` |
| `DataTransfer` | Vendor messages (last 20) | `/data_transfer` |

## Install

1. HA: **Settings > Add-ons > Add-on Store > ⋮ > Repositories**
2. Add: `https://github.com/nickveenhof/ocpp-sniffer`
3. Install **OCPP Sniffer**, configure, start.

## Config

```yaml
upstream_url: "wss://your-cpo-endpoint/ocpp/YOUR_CHARGER_ID"
charger_password: "your-password"
min_current: 6
auto_throttle: true
```

| Field | Required | Default | Description |
|---|---|---|---|
| `upstream_url` | Yes | | Your CPO OCPP WebSocket URL |
| `charger_password` | Recommended | | OCPP Basic Auth password. Set the same value in your charger's OCPP password field. |
| `min_current` | No | 6 | Minimum charge current in amps. Used by `/enable/true`. |
| `auto_throttle` | No | true | On `StartTransaction`, immediately set current to 0A. Prevents charging until evcc sends `/enable/true`. |

## Auto-throttle

When `auto_throttle: true`, the sniffer injects a `SetChargingProfile` with 0A immediately after `StartTransaction`. The CPO starts the session (billing runs), but the charger draws no power. evcc decides when and how fast to charge via `/enable/true` and `/maxcurrent/{amps}`.

Without auto-throttle, the charger starts at full power as soon as the CPO authorizes. evcc can only react after its next poll cycle (10-30 seconds of unwanted charging).

## Making the sniffer reachable

Your charger connects over WSS with a valid TLS certificate. Use a **Cloudflare Tunnel**.

```
Charger ──wss──► ocpp.yourdomain.com  (Cloudflare edge, valid TLS)
                        │
                   Cloudflare Tunnel
                        │
                   HA host (LAN) :9000
```

### Step 1: Add your domain to Cloudflare

Free plan works. Either transfer nameservers or register a new domain. If your domain is at another registrar: disable DNSSEC first, change nameservers to Cloudflare's, re-enable DNSSEC in Cloudflare after activation.

### Step 2: Create a tunnel

1. [one.dash.cloudflare.com](https://one.dash.cloudflare.com) > **Networks > Tunnels > Create**
2. Connector: **Cloudflared**. Name: anything. Click **Save**.
3. Copy the tunnel token (`eyJ...`).

### Step 3: Install Cloudflared add-on in HA

1. HA: **Settings > Add-ons > Add-on Store > ⋮ > Repositories**
2. Add: `https://github.com/homeassistant-apps/repository`
3. Install **Cloudflared**. Set config:
   ```yaml
   tunnel_token: "eyJ...your token..."
   ```
4. Start. Cloudflare dashboard shows tunnel as **Connected**.

### Step 4: Add public hostname

Cloudflare Zero Trust > **Tunnels > your tunnel > Configure > Public Hostname > Add**:

| Field | Value |
|---|---|
| Subdomain | `ocpp` |
| Domain | `yourdomain.com` |
| Type | `HTTP` |
| URL | `SNIFFER_CONTAINER_IP:9000` |

Find the sniffer container IP:
```bash
docker inspect addon_XXXXX_ocpp-proxy \
  --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
```

### Step 5: Point your charger at the sniffer

| Setting | Value |
|---|---|
| OCPP URL | `wss://ocpp.yourdomain.com/charger` |
| Identity | your charger serial number |
| Password | your `charger_password` |

## REST API

Available at `http://SNIFFER_IP:9000`.

### Read

| Endpoint | Description |
|---|---|
| `GET /charger_info` | RFID tag, status, vendor, firmware |
| `GET /meter_values` | L1/L2/L3 voltage, current, power, energy |
| `GET /last_session` | Last session: idTag, energy, stop reason |
| `GET /data_transfer` | Last 20 vendor DataTransfer messages |
| `GET /status` | Upstream URL, connection state |
| `GET /sessions` | All sessions (JSON) |
| `GET /sessions.csv` | All sessions (CSV) |

### Commands

| Endpoint | OCPP command | Description |
|---|---|---|
| `POST /enable/true` | `SetChargingProfile` | Resume charging at `min_current` amps |
| `POST /enable/false` | `SetChargingProfile` | Pause charging (0A) |
| `POST /maxcurrent/{amps}` | `SetChargingProfile` | Set max current |
| `POST /command` | any | `{"action":"...","payload":{...}}` |

## evcc config

```yaml
chargers:
  - name: wallbox
    type: custom

    status:
      source: http
      uri: http://SNIFFER_IP:9000/charger_info
      jq: .evcc_status

    enabled:
      source: http
      uri: http://SNIFFER_IP:9000/charger_info
      jq: .last_status != "Unavailable"

    enable:
      source: http
      uri: http://SNIFFER_IP:9000/enable/{{.enable}}
      method: POST

    maxcurrent:
      source: http
      uri: http://SNIFFER_IP:9000/maxcurrent/{{.maxcurrent}}
      method: POST

    power:
      source: http
      uri: http://SNIFFER_IP:9000/meter_values
      jq: .power_w

    energy:
      source: http
      uri: http://SNIFFER_IP:9000/meter_values
      jq: .energy_wh / 1000

    identify:
      source: http
      uri: http://SNIFFER_IP:9000/charger_info
      jq: .last_id_tag

    currents:
      - source: http
        uri: http://SNIFFER_IP:9000/meter_values
        jq: .current_l1
      - source: http
        uri: http://SNIFFER_IP:9000/meter_values
        jq: .current_l2
      - source: http
        uri: http://SNIFFER_IP:9000/meter_values
        jq: .current_l3

    voltages:
      - source: http
        uri: http://SNIFFER_IP:9000/meter_values
        jq: .voltage_l1
      - source: http
        uri: http://SNIFFER_IP:9000/meter_values
        jq: .voltage_l2
      - source: http
        uri: http://SNIFFER_IP:9000/meter_values
        jq: .voltage_l3
```

Replace `SNIFFER_IP` with your sniffer container IP.

## Vehicle identification

```yaml
vehicles:
  - name: polestar4
    type: polestar
    identifiers:
      - 97BA7F51
```

### Finding your RFID tag

1. Point charger at sniffer.
2. Plug in your car (tag appears in `StartTransaction`).
3. `GET /charger_info` → `.last_id_tag`.

## Notes

**One upstream only.** No multi-backend support.

**Local auth.** CPOs using `SendLocalList` authorize locally. The idTag still appears in `StartTransaction` at plug-in.

**MeterValues.** Returns zeros until a charging session starts.

**BootNotification.** Vendor/model/firmware populate on full power cycle only.

**Tested with.** Wallbox Pulsar Pro + Wattify CPO. Other OCPP 1.6 chargers and CPOs should work but are untested.

## License

MIT
