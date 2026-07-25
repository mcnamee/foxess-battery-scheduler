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
  `_source` string that reports which path was used.
- **New subcommands** go in `main()`'s `subparsers`, get a `cmd_*` handler with
  the `(args, config, log)` signature, and are registered in the `handlers`
  dict. `force-charge` is an alias that rewrites `args.mode`/`args.command`
  before dispatch — follow that pattern for other aliases.
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

- Base URL `https://www.foxesscloud.com`; endpoints under `/op/v0/device/...`.
- Auth is a header triple: `token` (the API key), `timestamp` (ms), and
  `signature` = `md5(path \r\n token \r\n timestamp)`. A wrong system clock
  produces errno `40256`.
- **1440 API calls per day**, and query endpoints tolerate roughly **1 request
  per second** — `cmd_settings()` sleeps 1.1s between reads for this reason.
  Don't add chatty polling loops.
- Writing schedule groups is `scheduler/enable` with a `groups` list; an empty
  list clears the schedule.
- Scheduler windows **cannot span midnight** — `build_group()` rejects
  `end <= start` on purpose. Callers needing an overnight window must issue two
  windows.

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
