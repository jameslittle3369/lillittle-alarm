#!/usr/bin/env python3
"""envisalink_2_postgres.py -- feed alarm zone/partition/trouble events
from an EnvisaLink EVL3/4 module into sensors-backend-fastapi.

This panel turned out to speak EnvisaLink's **Ademco/Honeywell Vista**
TPI variant, not DSC's numbered-code TPI protocol (confirmed live: the
device sends a plaintext "Login:" prompt and "%CC,DATA$"-style status
lines carrying literal keypad display text, e.g.
"%00,01,5C28,08,00, DISARMED CHIME   Ready to Arm  $" -- nothing like
DSC's "CCCDDDCKS" checksummed numeric-code format). ADT historically
resold rebranded Honeywell/Ademco Vista panels (e.g. "Safewatch Pro"),
which is almost certainly what's actually installed here despite it
being sold/thought-of as "a DSC system."

Protocol details below are verified against the official EnvisaLink TPI
Programmer's Document (Ademco edition, v1.03) -- specifically cross-
checked by decoding this panel's own live "5C28"/"5C08" icon bitfields
against the doc's bit table and confirming they match the keypad text
exactly (LOW BATTERY bit <-> "SYSTEM LO BAT"; CHIME+READY bits <->
"DISARMED CHIME Ready to Arm") -- not guessed.

Unlike the other sensors-apps (one-shot "poll vendor API, push, exit"
scripts run on a systemd timer), this protocol is inherently push-based
over a persistent TCP connection (default port 4025) -- the panel sends
an unsolicited status line the instant something changes. So this
script runs continuously (systemd service, not timer, matching
rtl-postgres's shape), maintaining that one connection and pushing each
event to the API as it arrives.

Only EnvisaLink's own TPI password is needed (the same password used to
log into the EnvisaLink's local web page) -- there is no username field
at all in this protocol, and no DSC-style separate panel access code is
needed since this app only listens for status broadcasts; it never
arms, disarms, or bypasses anything.

This protocol doesn't expose zone labels/types at all, only zone
numbers -- see zone_map.json (and the optional partition_map.json) for
supplying your own numbers -> names/types.

Usage:
    python envisalink_2_postgres.py [--verbose] [--env-file PATH]

Configuration comes from a .env file (see .env.example) or the ambient
environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency guard
    sys.exit("Missing dependency 'python-dotenv'.  Run: pip install -r requirements.txt")

try:
    import requests
except ImportError:  # pragma: no cover - dependency guard
    sys.exit("Missing dependency 'requests'.  Run: pip install -r requirements.txt")

# ---- TPI message format (EnvisaLink TPI Programmer's Document, Ademco
# edition v1.03): every status line is "%CC,DATA$" where CC is a 2-hex-
# digit command code. Login is plaintext password + CR -- no username,
# no checksum, no coded login-response message (just "OK"/"FAILED").
MESSAGE_RE = re.compile(r"^%([0-9A-Fa-f]{2}),(.*)\$$")

CMD_KEYPAD_UPDATE = "00"  # partition, 16-bit icon bitfield, zone/user, beep, 32-char display text
CMD_ZONE_STATE = "01"  # 8-byte (16 hex char) little-endian bitfield, one bit per zone 1-64
CMD_PARTITION_STATE = "02"  # 8-byte hex string, one status byte per partition 1-8
# CMD 03 (Realtime CID Event) and FF (Zone Timer Dump) are not handled --
# CID events carry richer per-event detail (specific trouble/alarm cause
# codes) but need their own verified code-table before trusting them;
# the icon bitfield + zone bitfield below already satisfy everything
# this app set out to log.

# Icon/LED bitfield bit positions (Virtual Keypad Update, command 00).
ICON_BIT_ARMED_STAY = 15
ICON_BIT_LOW_BATTERY = 14
ICON_BIT_FIRE = 13
ICON_BIT_READY = 12
ICON_BIT_CHECK_TROUBLE = 9
ICON_BIT_ARMED_ZERO_ENTRY = 7
ICON_BIT_CHIME = 5
ICON_BIT_BYPASS = 4
ICON_BIT_AC_PRESENT = 3
ICON_BIT_ARMED_AWAY = 2
ICON_BIT_ALARM_MEMORY = 1
ICON_BIT_ALARM = 0

# Confirmed partition-status byte values (command 02); the doc mentions
# a fuller 00-10 range but only documents these explicitly -- anything
# else is reported as a raw "status_<hex>" string rather than guessed.
PARTITION_STATUS = {
    "01": "ready",
    "04": "armed_stay",
    "05": "armed_away",
    "08": "alarm",
}

RECONNECT_MIN = 2.0
RECONNECT_MAX = 128.0
LOGIN_TIMEOUT = 10.0
# No client-initiated keepalive in this protocol variant (unlike DSC's
# "000" command) -- the panel broadcasts keypad updates on its own as
# the display refreshes, which keeps the connection naturally active.
# This is just the dead-connection safety net: no traffic at all for
# this long means the connection is actually dead.
READ_TIMEOUT = 300.0


def load_json_map(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: failed to read {path}: {exc}", file=sys.stderr)
        return {}


@dataclass
class Config:
    host: str
    port: int
    password: str
    api_base_url: str
    zone_map: dict[str, dict[str, str]] = field(default_factory=dict)
    partition_map: dict[str, str] = field(default_factory=dict)
    verbose: bool = False

    @classmethod
    def from_env(cls, env_file: str, verbose: bool) -> "Config":
        load_dotenv(env_file)

        host = os.getenv("ENVISALINK_HOST", "").strip()
        password = os.getenv("ENVISALINK_PASSWORD", "")
        api_base_url = os.getenv("API_BASE_URL", "").rstrip("/")
        if not host:
            sys.exit("ENVISALINK_HOST must be set in .env")
        if not password:
            sys.exit("ENVISALINK_PASSWORD must be set in .env")
        if not api_base_url:
            sys.exit("API_BASE_URL must be set in .env")

        zone_map = load_json_map(os.getenv("ZONE_MAP_FILE", "zone_map.json"))
        partition_map_raw = load_json_map(os.getenv("PARTITION_MAP_FILE", "partition_map.json"))
        partition_map = {
            k: (v.get("name", k) if isinstance(v, dict) else str(v))
            for k, v in partition_map_raw.items()
        } or {"1": "Main"}

        return cls(
            host=host,
            port=int(os.getenv("ENVISALINK_PORT", "4025")),
            password=password,
            api_base_url=api_base_url,
            zone_map=zone_map,
            partition_map=partition_map,
            verbose=verbose,
        )


class Pusher:
    """POSTs one event to one endpoint. A plain synchronous `requests`
    call made from inside the async loop -- zone/partition/trouble events
    are infrequent (not a per-second stream like rtl_433), so blocking
    the loop for one HTTP request isn't worth an async HTTP client
    dependency just for this."""

    def __init__(self, api_base_url: str, verbose: bool) -> None:
        self.api_base_url = api_base_url
        self.verbose = verbose

    def push(self, path: str, body: dict[str, Any], label: str) -> None:
        url = f"{self.api_base_url}/{path}"
        try:
            response = requests.post(url, json=body, timeout=10)
            response.raise_for_status()
            if self.verbose:
                print(f"{label}: created={response.json().get('created')}")
        except requests.RequestException as exc:
            print(f"error posting to {path}: {exc}", file=sys.stderr)


class EnvisalinkClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.pusher = Pusher(config.api_base_url, config.verbose)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # None until the first zone-state message arrives, so that first
        # message is treated as a baseline sync (no synthetic "open"
        # events for zones that were already open before this connection
        # started) rather than every currently-open zone looking "new".
        self._last_open_zones: set[int] | None = None

    def log(self, msg: str) -> None:
        if self.config.verbose:
            # Scrub the TPI password if it ever ends up in a log line.
            print(msg.replace(self.config.password, "*" * len(self.config.password)))

    async def run(self) -> None:
        """Connect, log in, and listen forever -- reconnecting with
        exponential backoff on any failure. systemd's Restart=always is
        the final safety net, but reconnecting ourselves first avoids a
        full process restart (and the "service flapping" look in
        `systemctl status`) for an ordinary dropped connection."""
        reconnect_delay = RECONNECT_MIN
        while True:
            try:
                await self._connect_and_listen()
                reconnect_delay = RECONNECT_MIN  # clean disconnect, not a failure
            except (OSError, asyncio.IncompleteReadError, TimeoutError) as exc:
                print(f"connection lost: {exc}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 -- keep the service alive; log and retry
                print(f"unexpected error: {exc}", file=sys.stderr)
            self._last_open_zones = None
            print(f"reconnecting in {reconnect_delay:.0f}s...", file=sys.stderr)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX)

    async def _connect_and_listen(self) -> None:
        self.log(f"connecting to {self.config.host}:{self.config.port}")
        reader, writer = await asyncio.open_connection(self.config.host, self.config.port)
        self._reader, self._writer = reader, writer
        try:
            await self._login()
            await self._read_loop()
        finally:
            writer.close()

    async def _login(self) -> None:
        # No username field, no coded command, no checksum in this
        # protocol variant -- just the plaintext password + CR. We can
        # send it immediately without waiting to see the "Login:" banner
        # first (TCP is full-duplex; the panel doesn't care what order
        # the two directions' bytes arrive in), which sidesteps relying
        # on matching that banner's exact text.
        assert self._writer is not None
        self._writer.write(f"{self.config.password}\r\n".encode("ascii"))
        await self._writer.drain()

        deadline = asyncio.get_event_loop().time() + LOGIN_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise ConnectionError("timed out waiting for login response")
            line = await asyncio.wait_for(self._readline(), timeout=remaining)
            if line is None:
                raise ConnectionError("connection closed during login")
            if line.startswith("OK"):
                self.log("login successful")
                return
            if line.startswith("FAILED") or line.startswith("Timed Out"):
                raise ConnectionError(f"login failed: {line!r}")
            # Anything else (e.g. the "Login:" banner itself) is just
            # preamble -- keep reading until a real OK/FAILED arrives.

    async def _read_loop(self) -> None:
        while True:
            line = await asyncio.wait_for(self._readline(), timeout=READ_TIMEOUT)
            if line is None:
                raise ConnectionError("EnvisaLink closed the connection")
            self._dispatch(line)

    async def _readline(self) -> str | None:
        assert self._reader is not None
        try:
            raw = await self._reader.readuntil(b"\n")
        except asyncio.IncompleteReadError:
            return None
        if not raw:
            return None
        return raw.decode("ascii", errors="replace").strip()

    def _dispatch(self, line: str) -> None:
        match = MESSAGE_RE.match(line)
        if not match:
            self.log(f"<- (unrecognized) {line}")
            return
        code, data = match.group(1).upper(), match.group(2)
        self.log(f"<- {code} {data}")
        if code == CMD_KEYPAD_UPDATE:
            self._handle_keypad_update(data)
        elif code == CMD_ZONE_STATE:
            self._handle_zone_state(data)
        elif code == CMD_PARTITION_STATE:
            self._handle_partition_state(data)
        # Everything else (Realtime CID Event, Zone Timer Dump, etc.) is
        # intentionally not handled yet -- see the module docstring.

    def _handle_keypad_update(self, data: str) -> None:
        # partition,icon_bitfield_hex,zone_or_user_hex,beep,display_text
        parts = data.split(",", 4)
        if len(parts) != 5:
            return
        _partition_hex, icon_hex, _zone_user_hex, _beep_hex, _text = parts
        try:
            icon_bits = int(icon_hex, 16)
        except ValueError:
            return

        # Only the "something's wrong" bits become trouble events --
        # the normal/good state of each (AC present, no fire, etc.)
        # isn't something worth logging as its own event.
        if icon_bits & (1 << ICON_BIT_LOW_BATTERY):
            self.pusher.push("alarm-trouble/log", {"condition": "low_battery"}, "trouble low_battery")
        if icon_bits & (1 << ICON_BIT_FIRE):
            self.pusher.push("alarm-trouble/log", {"condition": "fire"}, "trouble fire")
        if not icon_bits & (1 << ICON_BIT_AC_PRESENT):
            self.pusher.push("alarm-trouble/log", {"condition": "ac_trouble"}, "trouble ac_trouble")
        if icon_bits & (1 << ICON_BIT_CHECK_TROUBLE):
            self.pusher.push("alarm-trouble/log", {"condition": "system_trouble"}, "trouble system_trouble")

    def _handle_zone_state(self, data: str) -> None:
        # 8-byte (16 hex char) little-endian bitfield, one bit per zone
        # 1-64: byte i (hex chars [2i:2i+2]) covers zones (8i+1)..(8i+8),
        # bit b (LSB-first within that byte) = zone (8i+b+1). Verified
        # against the doc's own example: "0100000000000080" = zones 1
        # and 64 open/faulted.
        open_zones: set[int] = set()
        for byte_index in range(0, len(data), 2):
            byte_hex = data[byte_index : byte_index + 2]
            try:
                byte_val = int(byte_hex, 16)
            except ValueError:
                continue
            zone_base = (byte_index // 2) * 8
            for bit in range(8):
                if byte_val & (1 << bit):
                    open_zones.add(zone_base + bit + 1)

        if self._last_open_zones is None:
            # First message on this connection: this is the current
            # state, not a batch of brand-new events.
            self._last_open_zones = open_zones
            return

        for zone_number in sorted(open_zones - self._last_open_zones):
            self._push_zone_event(zone_number, "open")
        for zone_number in sorted(self._last_open_zones - open_zones):
            self._push_zone_event(zone_number, "restored")
        self._last_open_zones = open_zones

    def _push_zone_event(self, zone_number: int, event: str) -> None:
        zone_key = str(zone_number)
        info = self.config.zone_map.get(zone_key, {})
        name = info.get("name", f"Zone {zone_number}")
        zone_type = info.get("type", "other")
        self.pusher.push(
            f"alarm-zones/{zone_key}/log",
            {"name": name, "zone_type": zone_type, "event": event},
            f"zone {zone_number} ({name})",
        )

    def _handle_partition_state(self, data: str) -> None:
        # 8-byte hex string, one status byte per partition (1-8) -- not
        # a bitfield like zone-state; each byte IS that partition's
        # status code directly.
        for byte_index in range(0, len(data), 2):
            partition_number = str(byte_index // 2 + 1)
            byte_hex = data[byte_index : byte_index + 2].upper()
            status = PARTITION_STATUS.get(byte_hex, f"status_{byte_hex}")
            name = self.config.partition_map.get(partition_number, f"Partition {partition_number}")
            self.pusher.push(
                f"alarm-partitions/{partition_number}/log",
                {"name": name, "status": status},
                f"partition {partition_number} ({name})",
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream alarm zone/partition/trouble events from an "
        "EnvisaLink module into sensors-backend-fastapi."
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print each event as it's pushed"
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file (default .env)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = Config.from_env(args.env_file, args.verbose)
    client = EnvisalinkClient(config)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
