# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

A single Python script, `foxess_schedule.py`, that reads and writes the battery
**scheduler** on a FoxESS inverter via the FoxESS Open Cloud API. Its purpose is
to grid-charge a home battery during an energy provider's off-peak windows,
which change from day to day — so it is normally driven by a Home Assistant
`shell_command` from an automation, and is also runnable standalone for testing.

There is no build step, no package manifest, and no test suite. The whole
project is the one script plus this documentation.

## Hard constraints — do not break these

1. **Standard library only.** The script runs inside the Home Assistant Core
   container, where third-party packages (including PyYAML) may not be
   importable. Do not add dependencies, do not add a `requirements.txt`, and do
   not `import` anything outside the stdlib. This is why HA config parsing is a
   deliberate line scanner rather than a YAML parse — leave it that way unless
   the constraint changes.

2. **Single file.** Keep everything in `foxess_schedule.py`. Deployment is
   "copy one file into `/config/scripts/`"; splitting it into modules breaks
   that. Add new functionality as new sections/subcommands in the same file.

3. **Python 3 baseline.** Assume an older Python 3 in the HA container. The code
   uses `.format()` rather than f-strings — match that style rather than
   modernising it.

4. **Never commit real credentials.** API keys, device serial numbers, and log
   output containing them must not land in the repo. Use obviously-fake
   placeholders (`xxxxxxxx-...`, `60Kxxxxxxxxxxx`) in docs and examples.

## Code layout

`foxess_schedule.py` is organised into commented sections, in order:

- **logging** — `Log`, a minimal append logger with crude size-based rotation to
  `.1`. Errors/warnings go to stderr, everything else to stdout; `--quiet`
  suppresses the echo so `shell_command` output stays clean.
- **config** — credential resolution. `discover_from_ha()` scrapes `apiKey` /
  `deviceSN` from a `macxq/foxess-ha` platform block, `_resolve_secret()`
  handles `!secret` refs, `load_config()` layers the sources.
- **api** — `sign()` (the FoxESS signature) and `api_post()` (the only HTTP
  path), plus `explain_errno()` for known FoxESS error codes.
- **helpers** — `parse_hhmm()` and `build_group()`, which builds the scheduler
  group payload.
- **commands** — `cmd_get`, `cmd_settings`, `cmd_set`, `cmd_clear`. Each takes
  `(args, config, log)`.
- **cli** — `main()` with argparse subcommands.

### Conventions to follow

- **Config precedence is deliberate:** HA auto-discovery → config file →
  environment variables, later winning. Preserve that ordering and the
  `_source` string that reports which path was used. Non-credential options
  layer the other way round, CLI winning: `--api-version` beats `api_version`
  beats `DEFAULT_API_VERSION`, and `--fd-pwr` beats `fd_pwr` beats
  `DEFAULT_FD_PWR`.
- **New subcommands** go in `main()`'s `subparsers`, get a `cmd_*` handler with
  the `(args, config, log)` signature, and are registered in the `handlers`
  dict. `force-charge` is an alias that rewrites `args.mode`/`args.command`
  before dispatch — follow that pattern for other aliases. Note the alias assigns
  a **list** (`["ForceCharge"]`) because `--mode` is per-window. `set` and
  `force-charge` share their arguments via the `add_window_args()` helper, so add
  new window options there rather than to one parser only.
- **Per-window options are repeatable `action="append"` lists.** `--start`/`--end`
  define the window count; `--max-soc`, `--fd-soc` and `--mode` are broadcast by
  `expand_per_window()`, which accepts one value for all windows or one per
  window and errors on any other count. `--min-soc-on-grid` and `--fd-pwr` are
  deliberately schedule-wide, not per-window. `check_windows_dont_overlap()`
  rejects overlapping windows; touching windows are allowed.
- **Every command must honour `--dry-run`.** It flows through to `api_post()`,
  which logs the payload and returns a synthetic success without sending. Any
  new API call must pass `dry_run=args.dry_run`.
- **Exit codes are part of the contract** because Home Assistant surfaces them:
  `2` HTTP error, `3` network error, `4` non-JSON response, `5` API errno.
  Reuse these rather than inventing new ones; `SystemExit("message")` is used
  for user/config errors.
- **Log before acting.** Each command logs its intent, then `api_post()` logs
  the request and the result. The log file is the only debugging aid a user has
  when this runs unattended, so keep it informative.
- **Failures should be explanatory.** When adding new FoxESS error handling,
  extend `explain_errno()` with a plain-English hint rather than surfacing a
  bare code.

## FoxESS API notes

**The authoritative spec is committed as `foxess-openapi.pdf`.** Read it rather
than guessing — the scheduler sections are around pages 104–136. `foxesscloud.com`
is often unreachable from sandboxed environments, so the PDF is the reference.

- Base URL `https://www.foxesscloud.com`; endpoints under `/op/v{0,1,2,3}/device/...`.
- Auth is a header triple: `token` (the API key), `timestamp` (ms), and
  `signature` = `md5(path \r\n token \r\n timestamp)`. A wrong system clock
  produces errno `40256`.
- **1440 API calls per day**, and query endpoints tolerate roughly **1 request
  per second** — `cmd_settings()` sleeps 1.1s between reads, and so does any
  command that makes two calls. Don't add chatty polling loops.
- Writing schedule groups is `scheduler/enable` with a `groups` list; an empty
  list clears the schedule. The list takes **several groups** — the spec's own
  v0/v1 examples send two with different times, modes and SoC values — and a
  write **replaces** the entire schedule, so every wanted window must be included
  each time. The per-device cap is `maxGroupCount` in v3's `scheduler/get`.
- Scheduler windows **cannot span midnight** — `build_group()` rejects
  `end <= start` on purpose. Callers needing an overnight window must issue two
  windows.
- A group has **no day-of-week field**, so whatever window is written repeats
  every day until cleared. Day-of-week scheduling has to live in the caller
  (e.g. a Home Assistant condition) plus a paired `clear`.

### Scheduler versions differ structurally

`SCHEDULER_API` maps version → paths, and `build_group()` shapes the payload per
version. Do not assume one shape:

- **v0** — marked *Deprecated* in the spec. Flat group, per-group `enable`, and
  **no `maxSoc`** field. Sending `maxSoc` here is silently dropped, which is why
  `--max-soc` used to do nothing.
- **v1** — flat group, per-group `enable`, plus `maxSoc`.
- **v3** — default. SoC/power fields nest inside **`extraParam`**, there is no
  per-group `enable`, and `isDefault` controls whether unspecified `extraParam`
  fields reset to system defaults (we send `false`).
- **v2** exists in the spec but is not implemented here; its schema was not
  verified, so it is deliberately absent from `SCHEDULER_API`.
- There are no v2/v3 master-switch endpoints, so v3 reuses the **v1** flag paths.

### fdSoc and fdPwr are Force *Charge* parameters too

The spec calls these **FC/FD** fields — they govern Force Charge as well as Force
Discharge. Getting this wrong is what made grid charging silently no-op:

- `fdPwr` — the ceiling on power in that mode. For `ForceCharge` it is the max AC
  power drawn **from the grid**, so `0` means "never import" and nothing charges.
  The real range is **per-device**: v3's `scheduler/get` reports it under
  `properties.fdPwr.range.max` (v3 also returns `maxGroupCount` there). The v0/v1
  sections state a flat 0–6000, so `FD_PWR_LEGACY_MAX` drives a warning when a
  larger value is sent to those versions. `DEFAULT_FD_PWR` is 10500 — don't lower
  it back to 6000 assuming the spec's v0 range applies everywhere.
- `fdSoc` — the SoC at which charging or discharging **stops**. For `ForceCharge`
  that is the target, so it defaults to `maxSoc`; defaulting it to the discharge
  floor stops charging immediately. Must be ≥ `minSocOnGrid`.

`resolve_force_params()` owns these defaults and warns on combinations that would
do nothing. There is **no** "charge from grid" boolean anywhere in the API.

### The master switch

`scheduler/set/flag` with `{deviceSN, enable}` controls "timing mode". Groups
written while it is off are stored but never act — indistinguishable from a
schedule that does nothing. `cmd_set` turns it on before writing and `cmd_clear`
turns it off after; `--no-master-switch` opts out, and `cmd_flag` reads it.

`workMode` enumeration is `SelfUse, Feedin, Backup, ForceCharge, ForceDischarge`.
`PeakShaving` is a device setting, not a scheduler mode; it stays accepted on the
CLI for backwards compatibility but warns.

## Testing changes

There is no automated test suite. Verify changes by:

1. `python3 foxess_schedule.py <command> --dry-run` — exercises config
   resolution, argument parsing, and payload construction without touching the
   inverter or burning API quota. Use this for the majority of verification.
2. `python3 foxess_schedule.py get` against a real device only when the change
   genuinely needs a live round-trip. Prefer `--dry-run` given the daily call
   limit.
3. Credentials for local runs: export `FOXESS_API_KEY` and `FOXESS_DEVICE_SN`
   rather than writing a config file into the repo.

Gotchas when testing locally:

- `--dry-run`, `--quiet` and `--config` are **global** flags on the parent
  parser, so they must be passed *before* the subcommand
  (`foxess_schedule.py --dry-run force-charge ...`). Putting them after the
  subcommand is an argparse error.
- `--config` defaults to `/config/scripts/foxess.conf` and the log defaults to
  `/config/scripts/foxess.log`, neither of which exists outside Home Assistant.
  The logger will silently `makedirs` a real `/config/scripts` if the process
  can write to `/`, so pass `--config` and set `log_file` to keep test runs out
  of the filesystem root. Where it is not writable you get a benign
  "could not write log file" warning on stderr and the run still succeeds.

## Documentation

`README.md` documents what the script does, the optional `foxess-ha`
relationship, installation, and configuration. If you change subcommands,
flags, config keys, or credential resolution, update the README in the same
change — it is the only user-facing documentation.
