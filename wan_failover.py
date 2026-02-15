#!/usr/bin/env python3
"""
WAN Failover Daemon
===================
Automatically switches between two WAN connections based on health checks.
Provides a REST API for Home Assistant integration (monitoring, manual switch, auto toggle).

Gateway IPs are discovered automatically from state files written by the
bundled dhclient exit hook (/var/lib/wan-failover/<interface>.json), with
a fallback to routing-table parsing.

Usage:
    sudo python3 wan_failover.py --config /etc/wan-failover/config.yaml

Requires: root privileges (for ip route manipulation)
"""

import argparse
import hmac
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class WANLink:
    name: str              # internal id: "primary" / "secondary"
    interface: str         # e.g. "eth0"
    priority: int          # lower = more preferred
    display_name: Optional[str] = None  # friendly label, e.g. "Telekom"
    gateway: Optional[str] = None       # auto-detected
    # Runtime state
    healthy: bool = True               # internet reachable via this link
    gateway_reachable: bool = True     # gateway itself responds to ping
    last_check: Optional[float] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0

    @property
    def label(self) -> str:
        """Human-readable label: display_name if set, otherwise name."""
        return self.display_name or self.name


@dataclass
class Config:
    primary: WANLink = field(default_factory=lambda: WANLink("primary", "eth0", 1))
    secondary: WANLink = field(default_factory=lambda: WANLink("secondary", "eth1", 2))
    # Health check targets — multiple targets to avoid false positives
    ping_targets: list = field(default_factory=lambda: ["8.8.8.8", "1.1.1.1", "9.9.9.9"])
    ping_timeout: float = 1.5          # seconds per ping
    ping_count: int = 1                # pings per target
    check_interval: float = 3.0        # seconds between health checks
    # How many consecutive failures before declaring a link dead
    failure_threshold: int = 3
    # How many consecutive successes on preferred link before switching back
    recovery_threshold: int = 5
    # Minimum time (seconds) to stay on secondary before trying to switch back
    min_secondary_time: float = 30.0
    # Gateway state directory (dhclient hook writes here)
    gateway_state_dir: str = "/var/lib/wan-failover"
    # REST API
    api_host: str = "0.0.0.0"
    api_port: int = 8780
    api_key: str = "CHANGE_ME_TO_A_RANDOM_STRING"
    # Logging
    log_file: str = "/var/log/wan-failover.log"
    log_level: str = "INFO"


def load_config(path: str) -> Config:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    cfg = Config()

    if "primary" in raw:
        p = raw["primary"]
        cfg.primary = WANLink(
            name=p.get("name", "primary"),
            interface=p.get("interface", "eth0"),
            priority=1,
            display_name=p.get("display_name"),
        )
    if "secondary" in raw:
        s = raw["secondary"]
        cfg.secondary = WANLink(
            name=s.get("name", "secondary"),
            interface=s.get("interface", "eth1"),
            priority=2,
            display_name=s.get("display_name"),
        )

    cfg.ping_targets = raw.get("ping_targets", cfg.ping_targets)
    cfg.ping_timeout = raw.get("ping_timeout", cfg.ping_timeout)
    cfg.ping_count = raw.get("ping_count", cfg.ping_count)
    cfg.check_interval = raw.get("check_interval", cfg.check_interval)
    cfg.failure_threshold = raw.get("failure_threshold", cfg.failure_threshold)
    cfg.recovery_threshold = raw.get("recovery_threshold", cfg.recovery_threshold)
    cfg.min_secondary_time = raw.get("min_secondary_time", cfg.min_secondary_time)
    cfg.gateway_state_dir = raw.get("gateway_state_dir", cfg.gateway_state_dir)
    cfg.api_host = raw.get("api_host", cfg.api_host)
    cfg.api_port = raw.get("api_port", cfg.api_port)
    cfg.api_key = raw.get("api_key", cfg.api_key)
    cfg.log_file = raw.get("log_file", cfg.log_file)
    cfg.log_level = raw.get("log_level", cfg.log_level)

    return cfg


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(cfg: Config) -> logging.Logger:
    logger = logging.getLogger("wan_failover")
    logger.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    os.makedirs(os.path.dirname(cfg.log_file), exist_ok=True)
    fh = logging.FileHandler(cfg.log_file)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# Health checker — pings a target through a specific interface
# ---------------------------------------------------------------------------

def ping_target(target: str, interface: str, timeout: float = 1.5, count: int = 1, gateway: Optional[str] = None) -> bool:
    """
    Ping a target through a specific interface. Returns True if reachable.
    If gateway is provided, adds a temporary host route to ensure traffic uses that link.
    """
    route_added = False
    try:
        if gateway:
            # Force traffic to target via specific gateway to bypass default route lookup issues
            subprocess.run(
                ["ip", "route", "replace", target, "via", gateway, "dev", interface],
                capture_output=True, timeout=1
            )
            route_added = True

        result = subprocess.run(
            [
                "ping",
                "-c", str(count),
                "-W", str(int(timeout)),
                "-I", interface,
                target,
            ],
            capture_output=True,
            timeout=timeout + 2,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False
    finally:
        if route_added:
            try:
                subprocess.run(
                    ["ip", "route", "del", target, "via", gateway, "dev", interface],
                    capture_output=True, timeout=1
                )
            except Exception:
                pass


def check_internet_health(link: WANLink, targets: list, timeout: float, count: int) -> bool:
    """
    Check if a WAN link can reach the internet.
    Returns True if at least one target is reachable.
    """
    for target in targets:
        if ping_target(target, link.interface, timeout, count, gateway=link.gateway):
            return True
    return False


def check_gateway_health(link: WANLink, timeout: float) -> bool:
    """Ping the gateway itself (LAN-side check)."""
    if not link.gateway:
        return False
    return ping_target(link.gateway, link.interface, timeout, count=1)


# ---------------------------------------------------------------------------
# Gateway discovery
# ---------------------------------------------------------------------------

def read_gateway_from_state_file(interface: str, state_dir: str) -> Optional[str]:
    """
    Read gateway IP from the state file written by the dhclient exit hook.
    File: <state_dir>/<interface>.json  →  {"gateway": "x.x.x.x", ...}
    """
    path = os.path.join(state_dir, f"{interface}.json")
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("gateway")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def detect_gateway_from_routes(interface: str) -> Optional[str]:
    """
    Fallback: detect gateway from the routing table.
    Looks for 'default via X.X.X.X' or any 'via X.X.X.X' on the interface.
    """
    try:
        result = subprocess.run(
            ["ip", "route", "show", "dev", interface],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]

        # Prefer default route
        for line in lines:
            if line.startswith("default") and "via" in line:
                parts = line.split()
                return parts[parts.index("via") + 1]

        # Any route with a gateway
        for line in lines:
            if "via" in line:
                parts = line.split()
                return parts[parts.index("via") + 1]

    except Exception:
        pass
    return None


def discover_gateway(interface: str, state_dir: str) -> Optional[str]:
    """Try state file first, fall back to route parsing."""
    return read_gateway_from_state_file(interface, state_dir) or detect_gateway_from_routes(interface)


# ---------------------------------------------------------------------------
# Route management
# ---------------------------------------------------------------------------

def get_current_default_gateway() -> tuple[Optional[str], Optional[str]]:
    """Get the current default gateway IP and its interface."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            if line.startswith("default"):
                parts = line.split()
                gw = parts[parts.index("via") + 1] if "via" in parts else None
                dev = parts[parts.index("dev") + 1] if "dev" in parts else None
                return gw, dev
    except Exception:
        pass
    return None, None


def switch_default_route(link: WANLink, logger: logging.Logger) -> bool:
    """Switch the default route to use the given link."""
    if not link.gateway:
        logger.error(f"Cannot switch to {link.label}: no gateway detected for {link.interface}")
        return False
    try:
        # Remove existing default route(s)
        subprocess.run(
            ["ip", "route", "del", "default"],
            capture_output=True, timeout=5,
        )
        # Add new default route
        result = subprocess.run(
            ["ip", "route", "add", "default", "via", link.gateway, "dev", link.interface],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            logger.info(f"Default route switched to {link.label} ({link.gateway} via {link.interface})")
            return True
        else:
            logger.error(f"Failed to add route: {result.stderr.strip()}")
            return False
    except Exception as e:
        logger.error(f"Exception switching route: {e}")
        return False


# ---------------------------------------------------------------------------
# Failover Engine
# ---------------------------------------------------------------------------

class FailoverEngine:
    def __init__(self, cfg: Config, logger: logging.Logger):
        self.cfg = cfg
        self.logger = logger
        self.lock = threading.Lock()

        # State
        self.active_link: WANLink = cfg.primary
        self.auto_enabled: bool = True
        self.running: bool = False
        self.last_switch_time: float = 0.0
        self.switch_count: int = 0
        self.start_time: float = time.time()

        # Event history (last N events)
        self._event_log: list = []
        self._max_events: int = 100

        # Discover gateways and detect which link is currently active
        self._refresh_gateways()
        self._detect_active_link()

    def _refresh_gateways(self):
        """Discover/refresh gateway IPs for both links."""
        for link in (self.cfg.primary, self.cfg.secondary):
            gw = discover_gateway(link.interface, self.cfg.gateway_state_dir)
            if gw and gw != link.gateway:
                if link.gateway is not None:
                    self.logger.info(f"Gateway changed for {link.label}: {link.gateway} -> {gw}")
                    self._log_event("gateway_change", f"{link.label} gateway: {link.gateway} -> {gw}")
                else:
                    self.logger.info(f"Detected gateway for {link.label}: {gw} (via {link.interface})")
                link.gateway = gw
            elif not gw and link.gateway is not None:
                self.logger.warning(f"Lost gateway for {link.label} ({link.interface})")
                link.gateway = None

    def _detect_active_link(self):
        """Detect which link is currently in use based on default route."""
        _gw, dev = get_current_default_gateway()
        if dev == self.cfg.secondary.interface:
            self.active_link = self.cfg.secondary
            self.logger.info(f"Detected active link: {self.cfg.secondary.label} ({dev})")
        else:
            self.active_link = self.cfg.primary
            self.logger.info(f"Detected active link: {self.cfg.primary.label} ({dev})")

    def _log_event(self, event_type: str, message: str):
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "message": message,
        }
        self._event_log.append(entry)
        if len(self._event_log) > self._max_events:
            self._event_log = self._event_log[-self._max_events:]

    def _get_other_link(self, link: WANLink) -> WANLink:
        if link is self.cfg.primary:
            return self.cfg.secondary
        return self.cfg.primary

    def _do_switch(self, target: WANLink, reason: str):
        """Perform the actual switch. Caller must hold self.lock."""
        old = self.active_link
        if switch_default_route(target, self.logger):
            self.active_link = target
            self.last_switch_time = time.time()
            self.switch_count += 1
            msg = f"Switched {old.label} -> {target.label}: {reason}"
            self.logger.warning(msg)
            self._log_event("switch", msg)
        else:
            msg = f"FAILED to switch to {target.label}: {reason}"
            self.logger.error(msg)
            self._log_event("error", msg)

    def manual_switch(self, target_name: str) -> dict:
        """Manually switch to a named link. Returns status dict."""
        with self.lock:
            if target_name == self.cfg.primary.name:
                target = self.cfg.primary
            elif target_name == self.cfg.secondary.name:
                target = self.cfg.secondary
            else:
                return {"success": False, "error": f"Unknown link: {target_name}"}

            if self.active_link is target:
                return {"success": True, "message": f"Already on {target.label}"}

            self._do_switch(target, "manual switch via API")
            return {"success": True, "active": self.active_link.name}

    def set_auto(self, enabled: bool):
        with self.lock:
            self.auto_enabled = enabled
            state = "enabled" if enabled else "disabled"
            self.logger.info(f"Auto-failover {state}")
            self._log_event("config", f"Auto-failover {state}")

    def _link_status(self, link: WANLink) -> dict:
        """Build status dict for a single link."""
        return {
            "name": link.name,
            "display_name": link.label,
            "interface": link.interface,
            "gateway": link.gateway,
            "gateway_ip": link.gateway,
            "healthy": link.healthy,
            "gateway_reachable": link.gateway_reachable,
            "consecutive_failures": link.consecutive_failures,
            "consecutive_successes": link.consecutive_successes,
            "last_check": datetime.fromtimestamp(link.last_check, tz=timezone.utc).isoformat() if link.last_check else None,
        }

    def get_status(self) -> dict:
        with self.lock:
            return {
                "active_link": self.active_link.name,
                "active_display_name": self.active_link.label,
                "active_interface": self.active_link.interface,
                "active_gateway": self.active_link.gateway,
                "auto_enabled": self.auto_enabled,
                "primary": self._link_status(self.cfg.primary),
                "secondary": self._link_status(self.cfg.secondary),
                "switch_count": self.switch_count,
                "last_switch": datetime.fromtimestamp(self.last_switch_time, tz=timezone.utc).isoformat() if self.last_switch_time else None,
                "uptime_seconds": int(time.time() - self.start_time),
                "recent_events": self._event_log[-20:],
            }

    def _check_and_update(self, link: WANLink) -> bool:
        """Check a link's health (gateway + internet) and update state."""
        # Gateway ping (diagnostic — does not affect failover decision)
        gw_ok = check_gateway_health(link, self.cfg.ping_timeout)
        if gw_ok != link.gateway_reachable:
            link.gateway_reachable = gw_ok
            if gw_ok:
                self.logger.info(f"{link.label} gateway {link.gateway} reachable")
            else:
                self.logger.warning(f"{link.label} gateway {link.gateway or '?'} unreachable")

        # Internet reachability (drives failover decisions)
        healthy = check_internet_health(
            link, self.cfg.ping_targets, self.cfg.ping_timeout, self.cfg.ping_count,
        )
        link.last_check = time.time()

        if healthy:
            link.consecutive_failures = 0
            link.consecutive_successes += 1
            if not link.healthy:
                link.healthy = True
                self.logger.info(f"{link.label} recovered (internet reachable)")
                self._log_event("recovery", f"{link.label} is back online")
        else:
            link.consecutive_successes = 0
            link.consecutive_failures += 1
            if link.healthy and link.consecutive_failures >= self.cfg.failure_threshold:
                link.healthy = False
                self.logger.warning(f"{link.label} declared DOWN after {link.consecutive_failures} failures")
                self._log_event("down", f"{link.label} is down")

        return healthy

    def run_check_cycle(self):
        """Run a single health check cycle. Called by the main loop."""
        with self.lock:
            # Refresh gateways (DHCP may have changed them)
            self._refresh_gateways()

            # Check both links (gateway + internet)
            self._check_and_update(self.cfg.primary)
            self._check_and_update(self.cfg.secondary)

            if not self.auto_enabled:
                return

            active = self.active_link
            other = self._get_other_link(active)

            # Case 1: Active link is down — switch to other if it's healthy
            if not active.healthy:
                if other.healthy:
                    self._do_switch(other, f"{active.label} is down, {other.label} is healthy")
                else:
                    self.logger.error("Both links appear to be down!")
                    self._log_event("error", "Both links are down")
                return

            # Case 2: We're on the non-preferred link, check if preferred is back
            preferred = self.cfg.primary
            if active is not preferred and preferred.healthy:
                time_on_secondary = time.time() - self.last_switch_time
                if (time_on_secondary >= self.cfg.min_secondary_time
                        and preferred.consecutive_successes >= self.cfg.recovery_threshold):
                    self._do_switch(preferred, f"{preferred.label} recovered and stable")

    def run(self):
        """Main loop."""
        self.running = True
        self.logger.info(
            f"WAN Failover started | "
            f"Primary: {self.cfg.primary.label} ({self.cfg.primary.interface}, gw={self.cfg.primary.gateway or 'detecting...'}) | "
            f"Secondary: {self.cfg.secondary.label} ({self.cfg.secondary.interface}, gw={self.cfg.secondary.gateway or 'detecting...'}) | "
            f"Check interval: {self.cfg.check_interval}s"
        )
        self._log_event("start", "Daemon started")

        while self.running:
            try:
                self.run_check_cycle()
            except Exception as e:
                self.logger.exception(f"Error in check cycle: {e}")
            time.sleep(self.cfg.check_interval)

    def stop(self):
        self.running = False
        self.logger.info("Daemon stopping")
        self._log_event("stop", "Daemon stopped")


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

class APIHandler(BaseHTTPRequestHandler):
    """Simple REST API for HA integration."""

    engine: FailoverEngine = None  # Set by factory
    api_key: str = ""

    def log_message(self, format, *args):
        """Redirect HTTP logs to our logger."""
        self.engine.logger.debug(f"HTTP: {format % args}")

    def _check_auth(self) -> bool:
        """Check API key from header or query param."""
        # Header: Authorization: Bearer <key>
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if hmac.compare_digest(token, self.api_key):
                return True

        # Query param: ?api_key=<key>
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
            for param in query.split("&"):
                if param.startswith("api_key="):
                    token = param[8:]
                    if hmac.compare_digest(token, self.api_key):
                        return True

        return False

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str):
        self._send_json({"error": message}, status)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _route_path(self) -> str:
        """Return path without query string."""
        return self.path.split("?")[0]

    def do_GET(self):
        if not self._check_auth():
            self._send_error(401, "Unauthorized")
            return

        path = self._route_path()

        if path == "/api/status":
            self._send_json(self.engine.get_status())
        elif path == "/api/health":
            # Simple health check for monitoring
            self._send_json({"status": "ok"})
        else:
            self._send_error(404, "Not found")

    def do_POST(self):
        if not self._check_auth():
            self._send_error(401, "Unauthorized")
            return

        path = self._route_path()

        if path == "/api/switch":
            try:
                body = self._read_body()
                target = body.get("target", "")
                if not target:
                    self._send_error(400, "Missing 'target' in body")
                    return
                result = self.engine.manual_switch(target)
                status = 200 if result.get("success") else 400
                self._send_json(result, status)
            except Exception as e:
                self._send_error(500, str(e))

        elif path == "/api/auto":
            try:
                body = self._read_body()
                enabled = body.get("enabled")
                if enabled is None:
                    self._send_error(400, "Missing 'enabled' in body")
                    return
                self.engine.set_auto(bool(enabled))
                self._send_json({"success": True, "auto_enabled": bool(enabled)})
            except Exception as e:
                self._send_error(500, str(e))

        else:
            self._send_error(404, "Not found")


def make_api_handler(engine: FailoverEngine, api_key: str):
    """Factory to create handler class with engine reference."""
    class Handler(APIHandler):
        pass
    Handler.engine = engine
    Handler.api_key = api_key
    return Handler


def run_api_server(engine: FailoverEngine, cfg: Config):
    handler = make_api_handler(engine, cfg.api_key)
    server = HTTPServer((cfg.api_host, cfg.api_port), handler)
    server.daemon_threads = True
    engine.logger.info(f"REST API listening on {cfg.api_host}:{cfg.api_port}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="WAN Failover Daemon")
    parser.add_argument(
        "--config", "-c",
        default="/etc/wan-failover/config.yaml",
        help="Path to config file (default: /etc/wan-failover/config.yaml)",
    )
    args = parser.parse_args()

    # Load config
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"Config file not found: {args.config}", file=sys.stderr)
        print("Create it from config.example.yaml", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        sys.exit(1)

    if cfg.api_key == "CHANGE_ME_TO_A_RANDOM_STRING":
        print("WARNING: Change the api_key in your config file!", file=sys.stderr)

    # Check root
    if os.geteuid() != 0:
        print("This daemon must run as root (needs ip route access)", file=sys.stderr)
        sys.exit(1)

    # Ensure state directory exists
    os.makedirs(cfg.gateway_state_dir, exist_ok=True)

    # Setup
    logger = setup_logging(cfg)
    engine = FailoverEngine(cfg, logger)

    # Signal handling
    def handle_signal(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Start API server in background thread
    api_thread = threading.Thread(target=run_api_server, args=(engine, cfg), daemon=True)
    api_thread.start()

    # Run main loop (blocking)
    engine.run()


if __name__ == "__main__":
    main()
