#!/usr/bin/env python3
"""envisalink_2_postgres.py -- feed DSC alarm zone/partition/trouble
events from an EnvisaLink EVL3/4 module into sensors-backend-fastapi.

Unlike the other sensors-apps (one-shot "poll vendor API, push, exit"
scripts run on a systemd timer), EnvisaLink's TPI protocol is inherently
push-based over a persistent TCP connection (default port 4025) -- the
panel sends an unsolicited status line the instant a zone or partition
changes. So this script runs continuously (systemd service, not timer,
matching rtl-postgres's shape), maintaining that one connection and
pushing each event to the API as it arrives.

No EnvisaLink/DSC third-party library is used -- this is a minimal,
hand-rolled TPI client, but the connection-handling logic (checksum
algorithm, login handshake, keepalive interval, exponential-backoff
reconnect, command/read timeouts, partial-read handling, credential
scrubbing in logs) is ported from reading pyenvisalink's actual source
(the library Home Assistant's own `envisalink` integration uses), not
guessed -- that library has absorbed years of real-world EnvisaLink
quirks that are worth keeping even without taking the dependency itself.

Only EnvisaLink's own TPI password is needed here (set on the EnvisaLink
module itself) -- the DSC panel's keypad access code is NOT required,
since this app only listens for status broadcasts; it never arms,
disarms, or bypasses anything.

DSC doesn't expose zone labels/types over the TPI protocol at all, only
zone numbers -- see zone_map.json (and the optional partition_map.json)
for supplying your own numbers -> names/types.

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

# ---- TPI command codes (EnvisaLink TPI Programmer's Document v1.08) ----
LOGIN_PROMPT = "505"
LOGIN_RESPONSE = "005"
KEEPALIVE = "000"

ZONE_EVENTS = {
    "601": "alarm",
    "602": "alarm_restore",
    "603": "tamper",
    "604": "tamper_restore",
    "605": "fault",
    "606": "fault_restore",
    "609": "open",
    "610": "restored",
    "832": "low_battery",
    "833": "low_battery_restore",
}

PARTITION_EVENTS = {
    "650": "ready",
    "651": "not_ready",
    "652": "armed",
    "654": "alarm",
    "655": "disarmed",
    "656": "exit_delay",
    "657": "entry_delay",
}

TROUBLE_EVENTS = {
    "800": "low_battery",
    "802": "ac_trouble",
    "806": "bell_trouble",
    "840": "trouble_led",
    "841": "trouble_clear",
}

RECONNECT_MIN = 2.0
RECONNECT_MAX = 128.0
LOGIN_TIMEOUT = 10.0
# Keepalive fires every 60s once logged in, so any silence much longer
# than that means the connection is actually dead, not just a quiet
# panel -- this is deliberately generous, not a tight request timeout.
READ_TIMEOUT = 180.0


def checksum(code: str, data: str) -> str:
    """TPI's own checksum: sum of the ASCII byte values of code+data, hex,
    last two characters. Ported from pyenvisalink's DSCClient.get_checksum."""
    total = sum(ord(c) for c in code + data)
    return f"{total:02X}"[-2:]


def build_command(code: str, data: str = "") -> bytes:
    return f"{code}{data}{checksum(code, data)}\r\n".encode("ascii")


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
        self._logged_in = False

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
            self._logged_in = False
            print(f"reconnecting in {reconnect_delay:.0f}s...", file=sys.stderr)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX)

    async def _connect_and_listen(self) -> None:
        self.log(f"connecting to {self.config.host}:{self.config.port}")
        reader, writer = await asyncio.open_connection(self.config.host, self.config.port)
        self._reader, self._writer = reader, writer
        try:
            await self._login()
            keepalive_task = asyncio.create_task(self._keepalive_loop())
            try:
                await self._read_loop()
            finally:
                keepalive_task.cancel()
        finally:
            writer.close()

    async def _login(self) -> None:
        line = await asyncio.wait_for(self._readline(), timeout=LOGIN_TIMEOUT)
        code, _data = self._parse_line(line) if line else ("", "")
        if code != LOGIN_PROMPT:
            raise ConnectionError(f"expected login prompt (505), got {code!r}")
        await self._send(LOGIN_RESPONSE, self.config.password)

        line = await asyncio.wait_for(self._readline(), timeout=LOGIN_TIMEOUT)
        code, data = self._parse_line(line) if line else ("", "")
        # 505/1 = password accepted, 505/0 = failed, 505/2 = timed out
        if code == LOGIN_PROMPT and data == "1":
            self._logged_in = True
            self.log("login successful")
        else:
            raise ConnectionError(f"login failed (code={code!r} data={data!r})")

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if self._logged_in:
                await self._send(KEEPALIVE, "")

    async def _read_loop(self) -> None:
        while True:
            line = await asyncio.wait_for(self._readline(), timeout=READ_TIMEOUT)
            if line is None:
                raise ConnectionError("EnvisaLink closed the connection")
            code, data = self._parse_line(line)
            self._dispatch(code, data)

    async def _readline(self) -> str | None:
        assert self._reader is not None
        try:
            raw = await self._reader.readuntil(b"\r\n")
        except asyncio.IncompleteReadError:
            return None
        if not raw:
            return None
        return raw.decode("ascii", errors="replace").strip()

    async def _send(self, code: str, data: str) -> None:
        assert self._writer is not None
        self._writer.write(build_command(code, data))
        await self._writer.drain()

    @staticmethod
    def _parse_line(line: str) -> tuple[str, str]:
        # CCC DDD...DDD CKS -- code is always the first 3 characters,
        # checksum always the last 2; everything in between is data.
        # Incoming checksums aren't verified -- a corrupt line would just
        # fail to match a known code below and get silently dropped.
        return line[:3], line[3:-2]

    def _dispatch(self, code: str, data: str) -> None:
        self.log(f"<- {code} {data}")
        if code in ZONE_EVENTS:
            self._handle_zone_event(code, data)
        elif code in PARTITION_EVENTS:
            self._handle_partition_event(code, data)
        elif code in TROUBLE_EVENTS:
            self.pusher.push(
                "alarm-trouble/log",
                {"condition": TROUBLE_EVENTS[code]},
                f"trouble {TROUBLE_EVENTS[code]}",
            )
        # Every other code (command acks 500-502, keepalive ack, the
        # time/date broadcast 550, etc.) is intentionally ignored -- only
        # zone/partition/trouble status is what this app logs.

    def _handle_zone_event(self, code: str, data: str) -> None:
        # Zone data is 3-4 digits; the zone number is always the LAST 3
        # (confirmed against pyenvisalink's dsc_client.py:
        # `zoneNumber = int(data[-3:])`) -- no partition digit is mixed
        # in here, unlike partition-status messages below.
        digits = "".join(ch for ch in data if ch.isdigit())
        if len(digits) < 3:
            return
        zone_number = str(int(digits[-3:]))
        info = self.config.zone_map.get(zone_number, {})
        name = info.get("name", f"Zone {zone_number}")
        zone_type = info.get("type", "other")
        self.pusher.push(
            f"alarm-zones/{zone_number}/log",
            {"name": name, "zone_type": zone_type, "event": ZONE_EVENTS[code]},
            f"zone {zone_number} ({name})",
        )

    def _handle_partition_event(self, code: str, data: str) -> None:
        # Partition number is always the FIRST character of data
        # (confirmed against pyenvisalink: `partitionNumber = int(data[0])`);
        # code 652 (Armed) carries a 2nd byte for arm mode (stay/away/etc.)
        # that this app doesn't need -- it just logs "armed".
        if not data:
            return
        partition_number = data[0]
        name = self.config.partition_map.get(partition_number, f"Partition {partition_number}")
        self.pusher.push(
            f"alarm-partitions/{partition_number}/log",
            {"name": name, "status": PARTITION_EVENTS[code]},
            f"partition {partition_number} ({name})",
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream DSC alarm zone/partition/trouble events from an "
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
