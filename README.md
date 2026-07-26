# foxess-battery-scheduler

Control your FoxESS inverter's battery schedule from Home Assistant so you can
automatically charge from the grid during your energy provider's off-peak (or
free) windows — even when those windows move from day to day.

## What it does

`foxess_schedule.py` is a single-file, standard-library-only Python script that
talks to the [FoxESS Open Cloud API](https://www.foxesscloud.com) to read and
write the inverter's **scheduler**. It lets you:

- **`force-charge`** — set a one-off window in which the battery charges from the
  grid up to a target state of charge (e.g. charge to 100% between 12:00 and
  14:00 while your tariff is cheap or free).
- **`set`** — write a single-window schedule for any work mode in the scheduler
  enumeration (`SelfUse`, `Feedin`, `Backup`, `ForceCharge`, `ForceDischarge`).
- **`clear`** — remove all scheduler groups and switch the scheduler off, so the
  inverter reverts to its normal work-mode setting.
- **`get`** — read the schedule currently on the inverter.
- **`settings`** — read the current work mode and state-of-charge settings.
- **`flag`** — read, or explicitly set, the scheduler master switch.

The typical use case: your energy company publishes cheap/free off-peak periods
that change each day, so a Home Assistant automation calls this script with the
right start/end times to grid-charge the battery during that window, then clears
the schedule afterwards.

Every command supports `--dry-run` to log the request without sending it, which
is useful for testing automations. Note that `--dry-run`, `--quiet`, `--config`
and `--api-version` are global flags and must be placed **before** the
subcommand.

### How grid charging is actually controlled

There is no "charge from grid" boolean in the FoxESS API. Two parameters decide
whether a `ForceCharge` window does anything, and the API document describes
both as shared **FC/FD** fields — they apply to Force *Charge* as well as Force
*Discharge*:

| Parameter | Meaning in `ForceCharge` | Default here |
| --- | --- | --- |
| `fdPwr` | Ceiling on AC power the inverter may draw **from the grid**. `0` means it may not import at all, so nothing charges. | `6000` W |
| `fdSoc` | The SoC at which charging **stops**. Set below the current SoC and charging ends immediately. | same as `--max-soc` |

Both are settable with `--fd-pwr` / `--fd-soc`, and the script warns if you
choose values that would silently do nothing.

Separately, the scheduler has a **master switch** ("timing mode"). Groups
written while it is off are stored but never act. `set` / `force-charge` turn it
on and `clear` turns it off; use `--no-master-switch` to leave it untouched, or
the `flag` subcommand to inspect it.

## Dependency on the macxq foxess-ha HACS integration

This script does **not require** the
[macxq/foxess-ha](https://github.com/macxq/foxess-ha) HACS integration to run —
it is standalone and stdlib-only.

However, if you already have `foxess-ha` installed and configured, the script
will **auto-discover your credentials from it**, so there is nothing extra to
configure. On startup it scans your Home Assistant `configuration.yaml` (and
`sensors.yaml` / `sensor.yaml`) for the `foxess-ha` platform block and reads the
`apiKey` and `deviceSN`, resolving any `!secret` references against
`secrets.yaml`.

In short:

- **With `foxess-ha` already set up** → no configuration needed; credentials are
  reused automatically.
- **Without `foxess-ha`** → provide credentials via a config file or environment
  variables (see [How to configure](#how-to-configure)).

## How to install

The script only needs Python 3 (standard library only — no `pip install`
required).

1. **Create a `scripts` folder in your Home Assistant config directory.** It does
   not exist by default — `/config/scripts/` is simply the location this script
   defaults to. Put it alongside `configuration.yaml`, using whichever tool you
   have: create a new folder in the File Editor or Studio Code Server add-on,
   create one in the `config` Samba share, or run `mkdir -p /homeassistant/scripts`
   from the Terminal & SSH add-on.

   The file must live somewhere under the Home Assistant config directory.
   `shell_command` runs inside the HA Core container, and that directory is
   effectively the only persistent, writable path Core can see — a script in
   `/root/` or `/usr/local/bin/` would not be reachable.

   > **Do not use the `python_scripts/` folder.** That belongs to the
   > `python_script` integration, which runs a sandboxed Python that forbids
   > `import`. This script imports from the standard library and would fail
   > immediately. Use a plain folder such as `scripts/` and call it via
   > `shell_command`.

2. Copy `foxess_schedule.py` into that folder, e.g.
   `/config/scripts/foxess_schedule.py`.

3. Make it executable (optional, for standalone use):

   ```bash
   chmod +x /config/scripts/foxess_schedule.py
   ```

4. Register it as a `shell_command` in your `configuration.yaml`:

   ```yaml
   shell_command:
     foxess_force_charge: >-
       python3 /config/scripts/foxess_schedule.py --quiet
       force-charge --start {{ start }} --end {{ end }} --max-soc {{ max_soc }}
     foxess_clear: python3 /config/scripts/foxess_schedule.py --quiet clear
   ```

5. Restart Home Assistant (or reload `shell_command` configuration) so the new
   commands are available.

You can then call these from an automation. For example, to grid-charge to 100%
between 12:00 and 14:00 on Saturdays and Sundays:

```yaml
automation:
  # Saturdays and Sundays: put the 12:00-14:00 force-charge window in place.
  # Written a few minutes early so the schedule is live before the window opens.
  - alias: Weekend grid charge - set window
    trigger:
      - platform: time
        at: "11:55:00"
    condition:
      - condition: time
        weekday:
          - sat
          - sun
    action:
      - service: shell_command.foxess_force_charge
        data:
          start: "12:00"
          end: "14:00"
          max_soc: 100

  # Remove it once the window has passed, so it does not run on weekdays.
  - alias: Weekend grid charge - clear window
    trigger:
      - platform: time
        at: "14:05:00"
    condition:
      - condition: time
        weekday:
          - sat
          - sun
    action:
      - service: shell_command.foxess_clear
```

**Why two automations?** A scheduler group holds only start and end *times* —
there is no day-of-week field (see `build_group()`). Whatever window you write
stays on the inverter and repeats **every day** until it is cleared. So setting
a window on Saturday without clearing it would keep force-charging 12:00-14:00
on Monday through Friday too, quite possibly at peak rates. The second
automation is what makes it weekend-only.

If your off-peak window moves from day to day, replace the fixed `"12:00"` /
`"14:00"` above with templates drawn from whatever sensor or input helper holds
today's times, and keep the clear automation as-is.

### Which path do I use: `/config` or `/homeassistant`?

On Home Assistant OS the config directory is visible at **two different paths
depending on which container you are in** — they are the same folder:

| Where you are                        | Config directory appears as |
| ------------------------------------ | --------------------------- |
| HA Core (where `shell_command` runs) | `/config`                   |
| Add-ons (Terminal & SSH, etc.)       | `/homeassistant`            |

The practical rule:

- **In `configuration.yaml`, always write `/config/scripts/...`** — that is the
  path HA Core sees when it runs your `shell_command`.
- **In an add-on terminal, use whichever path that terminal shows you**
  (usually `/homeassistant/scripts/...`) when creating the folder or running
  the script by hand.

Credential auto-discovery handles both — the script probes `/config` and
`/homeassistant` in turn. But `--config` and the log file default to `/config/...`
specifically, so when testing from an add-on terminal you may need to pass
`--config /homeassistant/scripts/foxess.conf` there, while the `shell_command`
entry stays on `/config/...`.

### Testing from a terminal

The script is fully runnable standalone for testing. From the Terminal & SSH
add-on, check your placement worked before wiring up any automation:

```bash
python3 /homeassistant/scripts/foxess_schedule.py --dry-run get
```

You do not need to create `foxess.conf` or the log file first — the logger
creates its own directory, and if `foxess-ha` is already configured your
credentials are discovered automatically.

Other commands:

```bash
# Read the current schedule
python3 foxess_schedule.py get

# Force-charge to 100% between 12:00 and 14:00
python3 foxess_schedule.py force-charge --start 12:00 --end 14:00 --max-soc 100

# Preview a request without sending it (global flags come before the subcommand)
python3 foxess_schedule.py --dry-run force-charge --start 12:00 --end 14:00

# Remove all schedule groups
python3 foxess_schedule.py clear

# Read current work mode / SoC settings
python3 foxess_schedule.py settings
```

## How to configure

Credentials and options are resolved in the following order (**later sources win**):

1. **Auto-discovery from `foxess-ha`** — the `apiKey` and `deviceSN` from an
   existing `macxq/foxess-ha` platform block in your Home Assistant config
   (including `!secret` references resolved against `secrets.yaml`). If this is
   present, nothing else is needed.

2. **Config file** — `/config/scripts/foxess.conf` by default (override with
   `--config` or the `FOXESS_CONFIG` environment variable). Simple
   `key = value` format:

   ```ini
   api_key = xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   device_sn = 60Kxxxxxxxxxxx
   min_soc_on_grid = 10
   log_file = /config/scripts/foxess.log
   ```

3. **Environment variables** — `FOXESS_API_KEY` and `FOXESS_DEVICE_SN`.

### Scheduler API version

FoxESS has published several scheduler API versions and they are not
interchangeable. The default is **v3**, the current one.

| Version | Endpoint | Notes |
| --- | --- | --- |
| `v3` | `/op/v3/device/scheduler/enable` | Default. SoC and power fields nest inside `extraParam`; no per-group `enable`. |
| `v1` | `/op/v1/device/scheduler/enable` | Flat group with `maxSoc`. Use this if your inverter does not support v3. |
| `v0` | `/op/v0/device/scheduler/enable` | Marked **Deprecated** in the API document, and has **no `maxSoc` field** — `--max-soc` is silently ignored. The script warns if you select it. |

Set it per-run with `--api-version v1`, or persistently with `api_version = v1`
in the config file. If a write fails on v3, v1 is the fallback to try first.

The full API document is committed as [`foxess-openapi.pdf`](foxess-openapi.pdf);
the scheduler sections are around pages 104–136.

### Getting your credentials

- **API key** — generate an API key in the
  [FoxESS Cloud](https://www.foxesscloud.com) portal under your user profile /
  API management.
- **Device SN** — the serial number of your inverter, shown in the FoxESS Cloud
  app/portal.

### Configuration options

| Key               | Where           | Default                      | Description                                                                                                                     |
| ----------------- | --------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `api_key`         | file / env / HA | —                            | FoxESS Open API key (required).                                                                                                 |
| `device_sn`       | file / env / HA | —                            | Inverter serial number (required).                                                                                              |
| `min_soc_on_grid` | file            | `10`                         | Grid-present discharge floor, percent (10–100). Applies only to `set` / `force-charge`, and is overridden by `--min-soc-on-grid` if given. |
| `fd_pwr`          | file            | `6000`                       | Max force charge/discharge power in watts (0–6000). For `ForceCharge` this caps grid import. Overridden by `--fd-pwr`.           |
| `api_version`     | file            | `v3`                         | Scheduler API version: `v0`, `v1` or `v3`. Overridden by `--api-version`.                                                       |
| `log_file`        | file            | `/config/scripts/foxess.log` | Path for the append log (rotates to `.1` at ~1 MB).                                                                             |
| `log_max_bytes`   | file            | `1048576`                    | Log size before rotation.                                                                                                       |

### Command-line options

Global flags (must appear **before** the subcommand):

- `--config PATH` — path to the config file.
- `--dry-run` — log the request but do not send it.
- `--quiet` — log to file only, not stdout (recommended for `shell_command`).
- `--api-version {v0,v1,v3}` — scheduler API version (default `v3`).
- `--no-master-switch` — do not touch the scheduler master switch.

`set` / `force-charge` flags:

- `--start HH:MM` / `--end HH:MM` — the window (24-hour; must not span midnight).
- `--max-soc` — charge ceiling percent (default `100`). Ignored on `v0`.
- `--min-soc-on-grid` — grid-present discharge floor percent (10–100). Falls back
  to `min_soc_on_grid` from the config file, then `10`.
- `--mode` — work mode for `set` (default `ForceCharge`).
- `--fd-soc` — the SoC at which force charge/discharge **stops**. Defaults to
  `--max-soc` for `ForceCharge`, otherwise to the effective `min-soc-on-grid`.
  Must be ≥ `minSocOnGrid`.
- `--fd-pwr` — max force charge/discharge power in watts (0–6000). For
  `ForceCharge` this is the ceiling on **grid import**, so `0` disables charging.
  Falls back to `fd_pwr` from the config file, then `6000`.

`flag` flags: `--enable` / `--disable`. With neither, the switch is read.

### Notes and limits

- The FoxESS Open API allows **1440 calls per day**, and query endpoints are
  rate-limited to roughly **1 request per second** — keep automations modest.
  `set` / `force-charge` and `clear` each make **two** calls, since they also
  flip the master switch.
- Schedule windows cannot span midnight; split them into two windows if needed.
- A scheduler group has no day-of-week field, so a window repeats every day
  until it is cleared. See the weekend automation example above.
- `PeakShaving` is accepted on the command line but is not in the scheduler
  `workMode` enumeration, so the inverter may reject it. The script warns.
- The API signature depends on the system clock, so keep your host's time
  accurate (NTP).

## License

See [LICENSE](LICENSE).
