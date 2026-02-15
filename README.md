# WAN Failover Daemon

Automatic WAN failover daemon for Linux systems with dual internet connections. Monitors link health via ping checks and seamlessly switches the default route when the primary connection goes down. Includes a REST API for Home Assistant integration.

## Features

- **Automatic failover** — detects primary WAN failure and switches to secondary
- **Automatic recovery** — switches back to primary when it's stable again (configurable hysteresis)
- **Dynamic gateway detection** — discovers gateways via dhclient hook or routing table, works with DHCP
- **Two-level health monitoring** — tracks gateway reachability and internet connectivity separately
- **Interface traffic stats** — per-link RX/TX throughput in Mbps (instantaneous + rolling average)
- **Custom link names** — label your connections (e.g. "Telekom", "Digi") for readable logs and API
- **REST API** — status, manual switch, enable/disable auto-failover
- **Home Assistant ready** — integrates via REST sensors and switches
- **Systemd service** — runs as a daemon with auto-restart

## Requirements

- Linux with two WAN interfaces (DHCP)
- Python 3.9+
- Root privileges (for `ip route` manipulation)
- `pyyaml`

## Installation

```bash
git clone https://github.com/viroge/wan-failover-daemon.git
cd wan-failover-daemon
sudo bash install.sh
```

Then edit the config:

```bash
sudo nano /etc/wan-failover/config.yaml
```

Start the service:

```bash
sudo systemctl start wan-failover
sudo systemctl status wan-failover
```

## How It Works

### Gateway Discovery

Gateways are discovered automatically — no need to hardcode IPs:

1. **dhclient hook** (preferred) — the installer places an exit hook at `/etc/dhcp/dhclient-exit-hooks.d/wan-failover` that saves the DHCP-assigned gateway to `/var/lib/wan-failover/<interface>.json` on every lease event (BOUND/RENEW/REBIND).

2. **Routing table fallback** — if no state file exists, the daemon parses `ip route show dev <iface>` to find the gateway.

The daemon re-checks gateways every health check cycle, so it handles DHCP lease renewals that change the gateway.

### Health Monitoring

Each check cycle, the daemon evaluates two things per link:

| Check | What it tells you | Drives failover? |
|---|---|---|
| **Gateway ping** | LAN segment / CPE is alive | No (diagnostic only) |
| **Internet ping** | End-to-end connectivity works | Yes |

This means you can see in Home Assistant: "gateway is up but internet is down" (ISP outage) vs. "gateway is down" (local network issue).

## Configuration

See [config.example.yaml](config.example.yaml) for all options. Key settings:

| Parameter | Default | Description |
|---|---|---|
| `primary.interface` | `eth0` | Primary WAN interface |
| `primary.display_name` | — | Friendly label (e.g. "Telekom") |
| `secondary.interface` | `eth1` | Secondary WAN interface |
| `secondary.display_name` | — | Friendly label (e.g. "Digi") |
| `check_interval` | `3.0` | Seconds between health checks |
| `failure_threshold` | `3` | Consecutive failures before failover |
| `recovery_threshold` | `5` | Consecutive successes before switching back |
| `min_secondary_time` | `30.0` | Minimum seconds on secondary before trying primary |
| `gateway_state_dir` | `/var/lib/wan-failover` | Where dhclient hook writes gateway info |
| `api_port` | `8780` | REST API port |
| `api_key` | — | API authentication key (**change this!**) |

## REST API

All endpoints require authentication via `Authorization: Bearer <api_key>` header or `?api_key=<key>` query parameter.

### GET /api/status

Returns full daemon status including link health, gateway info, and recent events.

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" http://localhost:8780/api/status
```

Response includes per-link:
- `display_name` — friendly label
- `gateway` — current gateway IP (auto-detected)
- `healthy` — internet reachable (true/false)
- `gateway_reachable` — gateway responds to ping (true/false)
- `traffic.rx_mbps` / `traffic.tx_mbps` — instantaneous throughput (Mbps)
- `traffic.rx_mbps_avg` / `traffic.tx_mbps_avg` — rolling ~60s average (Mbps)

### GET /api/health

Simple health check endpoint.

```bash
curl -H "Authorization: Bearer YOUR_API_KEY" http://localhost:8780/api/health
```

### POST /api/switch

Manually switch to a specific link.

```bash
curl -X POST -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target": "secondary"}' \
  http://localhost:8780/api/switch
```

### POST /api/auto

Enable or disable automatic failover.

```bash
curl -X POST -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}' \
  http://localhost:8780/api/auto
```

## Home Assistant Integration

Add to your `configuration.yaml`:

```yaml
rest:
  - resource: http://YOUR_SERVER_IP:8780/api/status
    headers:
      Authorization: "Bearer YOUR_API_KEY"
    scan_interval: 10
    sensor:
      - name: "WAN Active Link"
        value_template: "{{ value_json.active_display_name }}"
      - name: "WAN Primary Gateway"
        value_template: >
          {{ value_json.primary.display_name }}:
          gw={{ value_json.primary.gateway or 'unknown' }}
        json_attributes_path: "$.primary"
        json_attributes:
          - healthy
          - gateway_reachable
          - gateway_ip
          - last_check
      - name: "WAN Secondary Gateway"
        value_template: >
          {{ value_json.secondary.display_name }}:
          gw={{ value_json.secondary.gateway or 'unknown' }}
        json_attributes_path: "$.secondary"
        json_attributes:
          - healthy
          - gateway_reachable
          - gateway_ip
          - last_check
      - name: "WAN Primary Healthy"
        value_template: "{{ value_json.primary.healthy }}"
      - name: "WAN Primary RX Mbps"
        value_template: "{{ value_json.primary.traffic.rx_mbps_avg }}"
        unit_of_measurement: "Mbps"
      - name: "WAN Primary TX Mbps"
        value_template: "{{ value_json.primary.traffic.tx_mbps_avg }}"
        unit_of_measurement: "Mbps"
      - name: "WAN Secondary Healthy"
        value_template: "{{ value_json.secondary.healthy }}"
      - name: "WAN Secondary RX Mbps"
        value_template: "{{ value_json.secondary.traffic.rx_mbps_avg }}"
        unit_of_measurement: "Mbps"
      - name: "WAN Secondary TX Mbps"
        value_template: "{{ value_json.secondary.traffic.tx_mbps_avg }}"
        unit_of_measurement: "Mbps"
      - name: "WAN Auto Failover"
        value_template: "{{ value_json.auto_enabled }}"
      - name: "WAN Switch Count"
        value_template: "{{ value_json.switch_count }}"

rest_command:
  wan_switch_primary:
    url: http://YOUR_SERVER_IP:8780/api/switch
    method: POST
    headers:
      Authorization: "Bearer YOUR_API_KEY"
      Content-Type: application/json
    payload: '{"target": "primary"}'

  wan_switch_secondary:
    url: http://YOUR_SERVER_IP:8780/api/switch
    method: POST
    headers:
      Authorization: "Bearer YOUR_API_KEY"
      Content-Type: application/json
    payload: '{"target": "secondary"}'

  wan_auto_on:
    url: http://YOUR_SERVER_IP:8780/api/auto
    method: POST
    headers:
      Authorization: "Bearer YOUR_API_KEY"
      Content-Type: application/json
    payload: '{"enabled": true}'

  wan_auto_off:
    url: http://YOUR_SERVER_IP:8780/api/auto
    method: POST
    headers:
      Authorization: "Bearer YOUR_API_KEY"
      Content-Type: application/json
    payload: '{"enabled": false}'
```

## File Layout

```
/opt/wan-failover/wan_failover.py                    # daemon
/etc/wan-failover/config.yaml                        # config
/etc/dhcp/dhclient-exit-hooks.d/wan-failover         # dhclient hook
/var/lib/wan-failover/<interface>.json                # gateway state (auto)
/var/log/wan-failover/wan-failover.log               # log
/etc/systemd/system/wan-failover.service             # systemd unit
```

## Logs

```bash
# Systemd journal
sudo journalctl -u wan-failover -f

# Log file
sudo tail -f /var/log/wan-failover.log
```

## License

[MIT](LICENSE)
