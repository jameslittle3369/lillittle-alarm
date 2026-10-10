# envisalink_2_postgres.py

Streams alarm panel activity -- door/window/motion zone open/close,
fire/low-battery/AC trouble conditions, and partition ready/armed/alarm
state -- into `sensors-backend-fastapi`, via an EnvisaLink EVL3/4
module's local TPI protocol (no cloud account, no vendor SDK).

This panel turned out to speak EnvisaLink's **Ademco/Honeywell Vista**
TPI variant, not DSC's -- confirmed live (plaintext `Login:` prompt,
`%CC,DATA$`-style status lines carrying literal keypad display text)
and cross-checked against the official Ademco TPI spec. ADT historically
resold rebranded Honeywell/Ademco Vista panels (e.g. "Safewatch Pro"),
which is almost certainly what's actually installed here despite it
being sold/thought-of as "a DSC system." See the module docstring in
`envisalink_2_postgres.py` for the full protocol breakdown.

Unlike the other `sensors-apps/*` scripts, this one runs **continuously**
as a long-lived process rather than on a timer: this protocol pushes an
unsolicited status line the instant something changes, so the app
maintains one persistent connection and reacts to events as they arrive,
instead of polling.

A Honeywell Tuxedo Touch panel was also evaluated as an alternative local
API for this same alarm system -- its local HTTP API was found to expose
only overall partition arm/disarm state, no per-zone detail and no event
log by any route, so it can't do what this app needs. EnvisaLink alone is
used here.

## Requirements

- Python 3.11+ (this fleet's venvs are 3.14)
- An EnvisaLink EVL3/EVL4 module already paired with the panel, with its
  own TPI password set (via the EnvisaLink's local web UI -- it's the
  same password used to log into that web UI)
- The panel's own keypad access code is **not** needed -- this app only
  listens, it never arms/disarms/bypasses anything

## Install

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configure

```bash
cp .env.example .env
cp zone_map.json.example zone_map.json
cp partition_map.json.example partition_map.json
```

Edit `.env` -- at minimum `ENVISALINK_HOST`, `ENVISALINK_PASSWORD`, and
`API_BASE_URL`. This protocol doesn't expose zone labels or types at
all, only zone numbers, so `zone_map.json` is how you tell this app
which zone number is which door/window/smoke detector (and
`partition_map.json` names your partition(s) -- most residential
installs have just one, and the default `{"1": "Main"}` is fine as-is).

## Usage

```bash
.venv/bin/python envisalink_2_postgres.py [options]
```

| Flag | Effect |
| --- | --- |
| *(none)* | Connect, log in, and stream events forever |
| `-v`, `--verbose` | Print each connection step and event as it's pushed (passwords scrubbed from any logged line) |
| `--env-file PATH` | Use a `.env` file other than `./.env` |

There's no `--push-api`/one-shot mode here, unlike the other apps --
pushing *is* what this script does, continuously, for as long as it runs.

## What gets logged

Every zone open/close change POSTs to
`{API_BASE_URL}/alarm-zones/{zone_number}/log` with
`{name, zone_type, event}`, where `event` is `open` or `restored`. This
protocol's zone-state message only distinguishes open/faulted vs.
restored (no separate fault/tamper/alarm distinction at this layer the
way DSC's protocol has) -- a dedicated alarm-event code (Realtime CID)
exists for richer detail but isn't parsed yet.

Every partition status change POSTs to
`{API_BASE_URL}/alarm-partitions/{partition_number}/log` with
`{name, status}`, where `status` is one of `ready`/`armed_stay`/
`armed_away`/`alarm` (confirmed values), or `status_<hex>` for any
status byte not yet confirmed against the spec.

Panel-wide trouble conditions -- derived from the keypad display's
icon/LED bitfield, not a dedicated trouble message -- POST to
`{API_BASE_URL}/alarm-trouble/log` with `{condition}`, where `condition`
is one of `low_battery`, `fire`, `ac_trouble`, or `system_trouble`.

## Scheduling

Not applicable -- this runs as a persistent systemd **service**
(`Restart=always`), not a timer. See `sensors-ansible/roles/app/tasks/
alarm.yml` for how the fleet deploys it.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Clean shutdown (shouldn't normally happen -- the connection loop runs forever) |
| `130` | Interrupted (Ctrl-C) |
| *(non-zero via `sys.exit`)* | Missing required `.env` setting |

Connection drops and login failures are **not** fatal -- they're logged
to stderr and retried with exponential backoff (2s up to 128s) from
inside the running process; systemd's `Restart=always` is the last-resort
safety net if the process itself dies.

## Troubleshooting

- `login failed: 'FAILED'` -- wrong `ENVISALINK_PASSWORD` (it's the
  EnvisaLink web UI's own login password, not any panel/keypad code).
- `timed out waiting for login response` -- wrong `ENVISALINK_PORT`, or
  something else is already connected to the EnvisaLink's single TPI
  session slot (it only accepts one client at a time).
- Events never arrive for a zone you know changed -- check
  `zone_map.json` has the right zone number, and check with `--verbose`
  for the raw `<- 01 ...`-style line (zone-state messages) to confirm
  what's actually being reported.

## Files

| File | Purpose |
| --- | --- |
| `envisalink_2_postgres.py` | The app itself |
| `.env.example` | Config template -- copy to `.env` |
| `zone_map.json.example` | Zone number -> name/type template -- copy to `zone_map.json` |
| `partition_map.json.example` | Partition number -> name template -- copy to `partition_map.json` |
| `requirements.txt` | Python dependencies |

## Security

`.env`, `zone_map.json`, and `partition_map.json` are all git-ignored --
they carry your EnvisaLink password and your real security zone layout
respectively. Only the `.example` templates are tracked.
