# envisalink_2_postgres.py

Streams DSC alarm panel activity -- door/window/motion zone open/close,
smoke/heat detector faults, partition arm/disarm state, and panel trouble
conditions -- into `sensors-backend-fastapi`, via an EnvisaLink EVL3/4
module's local TPI protocol (no cloud account, no vendor SDK).

Unlike the other `sensors-apps/*` scripts, this one runs **continuously**
as a long-lived process rather than on a timer: EnvisaLink's TPI protocol
pushes an unsolicited status line the instant something changes, so the
app maintains one persistent connection and reacts to events as they
arrive, instead of polling.

A Honeywell Tuxedo Touch panel was also evaluated as an alternative local
API for this same alarm system -- its local HTTP API was found to expose
only overall partition arm/disarm state, no per-zone detail and no event
log by any route, so it can't do what this app needs. EnvisaLink alone is
used here.

## Requirements

- Python 3.11+ (this fleet's venvs are 3.14)
- An EnvisaLink EVL3/EVL4 module already paired with the DSC panel, with
  its own TPI password set (via the EnvisaLink's local web UI)
- The DSC panel's own keypad access code is **not** needed -- this app
  only listens, it never arms/disarms/bypasses anything

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
`API_BASE_URL`. DSC doesn't expose zone labels or types over the TPI
protocol, only zone numbers, so `zone_map.json` is how you tell this app
which zone number is which door/window/smoke detector (and
`partition_map.json` names your partition(s) -- most residential DSC
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

Every zone status change POSTs to
`{API_BASE_URL}/alarm-zones/{zone_number}/log` with
`{name, zone_type, event}`, where `event` is one of `open`/`restored`,
`fault`/`fault_restore`, `alarm`/`alarm_restore`, `tamper`/
`tamper_restore`, or `low_battery`/`low_battery_restore`.

Every partition status change POSTs to
`{API_BASE_URL}/alarm-partitions/{partition_number}/log` with
`{name, status}`, where `status` is one of `ready`/`not_ready`/`armed`/
`alarm`/`disarmed`/`exit_delay`/`entry_delay`.

Panel-wide trouble conditions (AC loss, bell trouble, low battery, etc.
-- not tied to a specific zone or partition) POST to
`{API_BASE_URL}/alarm-trouble/log` with `{condition}`.

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

- `expected login prompt (505), got ...` -- wrong `ENVISALINK_PORT`, or
  something else is already connected to the EnvisaLink's single TPI
  session slot (it only accepts one client at a time).
- `login failed` -- wrong `ENVISALINK_PASSWORD`.
- Events never arrive for a zone you know changed -- check
  `zone_map.json` has the right zone number, and check with `--verbose`
  for the raw `<- 609 ...`-style line to confirm what number the panel
  is actually reporting.

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
