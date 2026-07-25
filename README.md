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
- **`set`** — write a single-window schedule for any supported work mode
  (`SelfUse`, `Feedin`, `Backup`, `ForceCharge`, `ForceDischarge`,
  `PeakShaving`).
- **`clear`** — remove all scheduler groups so the inverter reverts to its
  normal work-mode setting.
- **`get`** — read the schedule currently on the inverter.
- **`settings`** — read the current work mode and state-of-charge settings.

The typical use case: your energy company publishes cheap/free off-peak periods
that change each day, so a Home Assistant automation calls this script with the
right start/end times to grid-charge the battery during that window, then clears
the schedule afterwards.

Every command supports `--dry-run` to log the request without sending it, which
is useful for testing automations. Note that `--dry-run`, `--quiet` and
`--config` are global flags and must be placed **before** the subcommand.

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

1. Copy `foxess_schedule.py` into your Home Assistant config, e.g.
   `/config/scripts/foxess_schedule.py`.

2. Make it executable (optional, for standalone use):

   ```bash
   chmod +x /config/scripts/foxess_schedule.py
   ```

3. Register it as a `shell_command` in your `configuration.yaml`:

   ```yaml
   shell_command:
     foxess_force_charge: >-
       python3 /config/scripts/foxess_schedule.py --quiet
       force-charge --start {{ start }} --end {{ end }} --max-soc {{ max_soc }}
     foxess_clear: python3 /config/scripts/foxess_schedule.py --quiet clear
   ```

4. Restart Home Assistant (or reload `shell_command` configuration) so the new
   commands are available.

You can then call these from an automation — for example, grid-charge to 100%
during today's off-peak window:

```yaml
automation:
  - alias: Grid charge during off-peak
    trigger:
      - platform: time
        at: "12:00:00"
    action:
      - service: shell_command.foxess_force_charge
        data:
          start: "12:00"
          end: "14:00"
          max_soc: 100
```

### Testing from a terminal

The script is fully runnable standalone for testing:

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

### Getting your credentials

- **API key** — generate an API key in the
  [FoxESS Cloud](https://www.foxesscloud.com) portal under your user profile /
  API management.
- **Device SN** — the serial number of your inverter, shown in the FoxESS Cloud
  app/portal.

### Configuration options

| Key                | Where            | Default                        | Description                                                       |
| ------------------ | ---------------- | ------------------------------ | ----------------------------------------------------------------- |
| `api_key`          | file / env / HA  | —                              | FoxESS Open API key (required).                                   |
| `device_sn`        | file / env / HA  | —                              | Inverter serial number (required).                                |
| `min_soc_on_grid`  | file / CLI       | `10`                           | Grid-present discharge floor, percent.                            |
| `log_file`         | file             | `/config/scripts/foxess.log`   | Path for the append log (rotates to `.1` at ~1 MB).               |
| `log_max_bytes`    | file             | `1048576`                      | Log size before rotation.                                         |

### Command-line options

Global flags (must appear **before** the subcommand):

- `--config PATH` — path to the config file.
- `--dry-run` — log the request but do not send it.
- `--quiet` — log to file only, not stdout (recommended for `shell_command`).

`set` / `force-charge` flags:

- `--start HH:MM` / `--end HH:MM` — the window (24-hour; must not span midnight).
- `--max-soc` — charge ceiling percent (default `100`).
- `--min-soc-on-grid` — grid-present discharge floor percent.
- `--mode` — work mode for `set` (default `ForceCharge`).
- `--fd-soc` / `--fd-pwr` — force-discharge floor and power (unused for
  `ForceCharge`).

### Notes and limits

- The FoxESS Open API allows **1440 calls per day**, and query endpoints are
  rate-limited to roughly **1 request per second** — keep automations modest.
- Schedule windows cannot span midnight; split them into two windows if needed.
- The API signature depends on the system clock, so keep your host's time
  accurate (NTP).

## License

See [LICENSE](LICENSE).
