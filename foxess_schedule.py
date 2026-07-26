#!/usr/bin/env python3
"""
FoxESS Cloud scheduler control - single file, stdlib only.

Designed to be called from Home Assistant shell_command, but fully runnable
standalone from a terminal for testing.

Usage:
    foxess_schedule.py get
    foxess_schedule.py force-charge --start 12:00 --end 14:00 --max-soc 100
    foxess_schedule.py clear
    foxess_schedule.py settings          # read current WorkMode / SoC settings
    foxess_schedule.py flag              # read the scheduler master switch

--dry-run prints the request without sending it. It, --quiet, --config and
--api-version are global options, so they go BEFORE the subcommand, not after:
    foxess_schedule.py --dry-run force-charge --start 12:00 --end 14:00

The scheduler API has several versions. v3 is the default and current one; v0 is
deprecated and silently ignores maxSoc. Use --api-version v1 if your inverter
does not support v3.

Grid charging is controlled by fdPwr - the ceiling on AC power the inverter may
draw from the grid while in ForceCharge - and stops at fdSoc. Both default to
values that actually charge; passing --fd-pwr 0 disables force charging.

Credentials are resolved in this order (later wins):
    1. Auto-discovered from an existing macxq/foxess-ha platform block in
       /config/configuration.yaml (apiKey and deviceSN, including !secret refs
       resolved against secrets.yaml). Nothing to configure.
    2. /config/scripts/foxess.conf, if present - simple key=value:
           api_key = xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
           device_sn = 60Kxxxxxxxxxxx
           min_soc_on_grid = 10
           log_file = /config/scripts/foxess.log
    3. FOXESS_API_KEY / FOXESS_DEVICE_SN environment variables.

So with foxess-ha already configured, no config file is needed at all.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

BASE_URL = "https://www.foxesscloud.com"
DEFAULT_CONFIG = "/config/scripts/foxess.conf"
DEFAULT_LOG = "/config/scripts/foxess.log"
DEFAULT_LOG_MAX_BYTES = 1024 * 1024  # 1 MB before rotating to .1

PATH_SETTING_GET = "/op/v0/device/setting/get"

DEFAULT_API_VERSION = "v3"

# Scheduler endpoints per API version.
#
# v0 is marked "(Deprecated)" in the FoxESS Open API document and its group has
# no maxSoc field. v1 adds maxSoc as a flat group field. v3 is the current
# version: SoC/power fields move into a nested "extraParam" object and the
# per-group "enable" field is gone.
#
# There are no v2/v3 master-switch endpoints in the document, so those versions
# reuse the v1 flag paths.
SCHEDULER_API = {
    "v0": {
        "get": "/op/v0/device/scheduler/get",
        "enable": "/op/v0/device/scheduler/enable",
        "get_flag": "/op/v0/device/scheduler/get/flag",
        "set_flag": "/op/v0/device/scheduler/set/flag",
    },
    "v1": {
        "get": "/op/v1/device/scheduler/get",
        "enable": "/op/v1/device/scheduler/enable",
        "get_flag": "/op/v1/device/scheduler/get/flag",
        "set_flag": "/op/v1/device/scheduler/set/flag",
    },
    "v3": {
        "get": "/op/v3/device/scheduler/get",
        "enable": "/op/v3/device/scheduler/enable",
        "get_flag": "/op/v1/device/scheduler/get/flag",
        "set_flag": "/op/v1/device/scheduler/set/flag",
    },
}

# The scheduler workMode enumeration, per the API document. PeakShaving is a
# device setting rather than a scheduler mode - it is still accepted on the
# command line, but warned about, because it is not in this enumeration.
SCHEDULER_WORK_MODES = (
    "SelfUse",
    "Feedin",
    "Backup",
    "ForceCharge",
    "ForceDischarge",
)
WORK_MODES = SCHEDULER_WORK_MODES + ("PeakShaving",)

# Modes where fdSoc and fdPwr are meaningful. The API document describes both as
# "FC/FD" fields - they apply to Force Charge as well as Force Discharge.
FORCE_MODES = ("ForceCharge", "ForceDischarge")

# fdPwr is documented as ranging 0-6000. For ForceCharge it is the ceiling on AC
# power drawn from the grid, so 0 means "never import" and force charging simply
# does nothing. Default to the documented maximum and let the inverter clamp to
# whatever its hardware allows.
DEFAULT_FD_PWR = 6000
FD_PWR_MAX = 6000

# minSocOnGrid is documented as ranging 10-100.
MIN_SOC_ON_GRID_FLOOR = 10


# ---------------------------------------------------------------- logging

class Log:
    """Minimal append logger with crude size-based rotation."""

    def __init__(self, path, max_bytes=DEFAULT_LOG_MAX_BYTES, echo=True):
        self.path = path
        self.max_bytes = max_bytes
        self.echo = echo
        self._rotate_if_needed()

    def _rotate_if_needed(self):
        if not self.path:
            return
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def __call__(self, level, message):
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        line = "{} {:<7} {}".format(stamp, level, message)
        if self.echo:
            stream = sys.stderr if level in ("ERROR", "WARNING") else sys.stdout
            print(line, file=stream)
        if not self.path:
            return
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            print("could not write log file {}: {}".format(self.path, exc),
                  file=sys.stderr)

    def info(self, msg):
        self("INFO", msg)

    def warn(self, msg):
        self("WARNING", msg)

    def error(self, msg):
        self("ERROR", msg)


# ---------------------------------------------------------------- config

# HA OS exposes the same directory as /config (Core container) and
# /homeassistant (addon containers). Try both so the script runs either way.
HA_CONFIG_DIRS = ("/config", "/homeassistant")

# Files searched for an existing macxq/foxess-ha platform block
HA_CONFIG_CANDIDATES = (
    "configuration.yaml",
    "sensors.yaml",
    "sensor.yaml",
)


def _ha_dir():
    for directory in HA_CONFIG_DIRS:
        if os.path.isdir(directory):
            return directory
    return None


def _resolve_secret(value, ha_dir):
    """Resolve a '!secret foo' value against secrets.yaml."""
    if not value.startswith("!secret"):
        return value
    name = value.split(None, 1)[1].strip() if len(value.split(None, 1)) > 1 else ""
    if not name or not ha_dir:
        return None
    secrets_path = os.path.join(ha_dir, "secrets.yaml")
    if not os.path.exists(secrets_path):
        return None
    with open(secrets_path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, val = line.partition(":")
            if key.strip() == name:
                return val.strip().strip("'\"")
    return None


def discover_from_ha(ha_dir, log=None):
    """
    Scrape apiKey / deviceSN from an existing macxq/foxess-ha platform block
    so credentials live in one place only.

    Deliberately a line scanner, not a YAML parse - keeps this stdlib-only and
    avoids depending on PyYAML being importable in the Core container.

    Takes the first match found. If you ever run two inverters, set them
    explicitly in a config file instead.
    """
    if not ha_dir:
        return {}
    wanted = {"apikey": "api_key", "devicesn": "device_sn"}
    found = {}
    for filename in HA_CONFIG_CANDIDATES:
        path = os.path.join(ha_dir, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, value = line.partition(":")
            target = wanted.get(key.strip().lstrip("- ").lower())
            if not target or target in found:
                continue
            value = value.split("#")[0].strip().strip("'\"")
            if value.startswith("!secret"):
                value = _resolve_secret(value, ha_dir)
                if not value:
                    if log:
                        log.warn("could not resolve {} from secrets.yaml".format(key.strip()))
                    continue
            if value:
                found[target] = value
        if len(found) == len(wanted):
            break
    return found


def load_config(path, log=None):
    """
    Build config from, in order of precedence:
      1. environment (FOXESS_API_KEY, FOXESS_DEVICE_SN)
      2. the key=value config file, if it exists
      3. auto-discovery from Home Assistant's configuration.yaml / secrets.yaml
    """
    config = {}
    ha_dir = _ha_dir()

    discovered = discover_from_ha(ha_dir, log)
    config.update(discovered)

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip()
                if value:
                    config[key.strip().lower()] = value

    for env_name, key in (("FOXESS_API_KEY", "api_key"),
                          ("FOXESS_DEVICE_SN", "device_sn")):
        if os.environ.get(env_name):
            config[key] = os.environ[env_name]

    missing = [k for k in ("api_key", "device_sn") if not config.get(k)]
    if missing:
        raise SystemExit(
            "missing {}.\n"
            "Searched: {} (macxq foxess platform block), {} (key=value), "
            "and FOXESS_API_KEY / FOXESS_DEVICE_SN.".format(
                " and ".join(missing),
                os.path.join(ha_dir or "?", "configuration.yaml"),
                path,
            )
        )

    config["_source"] = "auto-discovered from HA config" if discovered and not os.path.exists(path) \
        else "config file / environment"
    return config


# ---------------------------------------------------------------- api

def sign(path, token, timestamp_ms):
    """FoxESS Open API signature: md5(path \\r\\n token \\r\\n timestamp)."""
    raw = "{}\r\n{}\r\n{}".format(path, token, timestamp_ms)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def api_post(path, token, payload, log, timeout=30, dry_run=False):
    timestamp_ms = str(int(time.time() * 1000))
    url = BASE_URL + path
    headers = {
        "Content-Type": "application/json",
        "token": token,
        "timestamp": timestamp_ms,
        "signature": sign(path, token, timestamp_ms),
        "lang": "en",
        "User-Agent": "ha-foxess-schedule/1.0",
    }
    body = json.dumps(payload).encode("utf-8")

    log.info("POST {} payload={}".format(path, json.dumps(payload)))
    if dry_run:
        log.info("dry-run - not sent")
        return {"errno": 0, "msg": "dry-run", "result": None}

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        log.error("HTTP {} from {} - {}".format(exc.code, path, detail))
        raise SystemExit(2)
    except urllib.error.URLError as exc:
        log.error("network error calling {} - {}".format(path, exc.reason))
        raise SystemExit(3)

    try:
        data = json.loads(text)
    except ValueError:
        log.error("non-JSON response from {}: {}".format(path, text[:500]))
        raise SystemExit(4)

    errno = data.get("errno", 0)
    if errno not in (0, None):
        log.error("API error {} ({}) from {}".format(
            errno, data.get("msg") or explain_errno(errno), path))
        raise SystemExit(5)

    log.info("OK {} result={}".format(path, json.dumps(data.get("result"))[:800]))
    return data


def explain_errno(errno):
    known = {
        40256: "signature or timestamp invalid - check system clock",
        40257: "token invalid or expired",
        40400: "daily API call limit (1440) exhausted",
        41930: "scheduler not supported by this firmware",
        44096: "unsupported function code - a schedule may already be active",
    }
    return known.get(errno, "unknown error code")


# ---------------------------------------------------------------- helpers

def parse_hhmm(value, label):
    try:
        hours, minutes = value.split(":")
        hour, minute = int(hours), int(minutes)
    except ValueError:
        raise SystemExit("{} must be in HH:MM 24-hour format, got '{}'".format(label, value))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SystemExit("{} out of range: {}".format(label, value))
    return hour, minute


def resolve_force_params(work_mode, max_soc, min_soc_on_grid, fd_soc, fd_pwr, log):
    """
    Work out fdSoc and fdPwr, which the API document describes as shared "FC/FD"
    fields - they govern Force Charge as well as Force Discharge:

      fdSoc - the SoC at which the system stops charging or discharging. For
              ForceCharge that is the charge target, so it defaults to maxSoc;
              defaulting it to the discharge floor stops charging immediately.
      fdPwr - the maximum power allowed in that mode. For ForceCharge it caps
              the AC power drawn from the grid, so 0 means the inverter may not
              import at all and nothing charges.
    """
    if fd_soc is None:
        fd_soc = max_soc if work_mode == "ForceCharge" else min_soc_on_grid
    if fd_pwr is None:
        fd_pwr = DEFAULT_FD_PWR

    if not (0 <= fd_soc <= 100):
        raise SystemExit("--fd-soc must be between 0 and 100, got {}".format(fd_soc))
    if fd_soc < min_soc_on_grid:
        raise SystemExit(
            "--fd-soc ({}) must be greater than or equal to minSocOnGrid ({})".format(
                fd_soc, min_soc_on_grid))
    if not (0 <= fd_pwr <= FD_PWR_MAX):
        raise SystemExit("--fd-pwr must be between 0 and {}, got {}".format(
            FD_PWR_MAX, fd_pwr))

    if work_mode in FORCE_MODES and fd_pwr == 0:
        log.warn("--fd-pwr is 0, so {} cannot move any power and will appear to "
                 "do nothing".format(work_mode))
    if work_mode == "ForceCharge" and fd_soc <= min_soc_on_grid:
        log.warn("--fd-soc ({}) is at or below minSocOnGrid ({}), so charging "
                 "will stop almost immediately".format(fd_soc, min_soc_on_grid))
    return fd_soc, fd_pwr


def build_group(version, start, end, work_mode, max_soc, min_soc_on_grid,
                fd_soc, fd_pwr):
    """
    Build one scheduler group in the shape the given API version expects.

    v0 has no maxSoc field at all - passing it there is silently ignored, which
    is why --max-soc used to have no effect. v3 nests the SoC and power fields
    inside extraParam and dropped the per-group enable flag.
    """
    start_hour, start_minute = parse_hhmm(start, "--start")
    end_hour, end_minute = parse_hhmm(end, "--end")
    if (end_hour, end_minute) <= (start_hour, start_minute):
        raise SystemExit("--end must be later than --start (no midnight-spanning windows)")

    group = {
        "startHour": start_hour,
        "startMinute": start_minute,
        "endHour": end_hour,
        "endMinute": end_minute,
        "workMode": work_mode,
    }

    if version == "v3":
        group["extraParam"] = {
            "minSocOnGrid": min_soc_on_grid,
            "fdSoc": fd_soc,
            "fdPwr": fd_pwr,
            "maxSoc": max_soc,
        }
        return group

    group["enable"] = 1
    group["minSocOnGrid"] = min_soc_on_grid
    group["fdSoc"] = fd_soc
    group["fdPwr"] = fd_pwr
    if version != "v0":  # v0 has no maxSoc
        group["maxSoc"] = max_soc
    return group


def set_master_switch(version, config, log, enable, dry_run):
    """
    Flip the scheduler master switch ("timing mode"). Groups written while this
    is off are stored but never act, which looks exactly like a schedule that
    does nothing.
    """
    log.info("setting scheduler master switch to {}".format(enable))
    api_post(SCHEDULER_API[version]["set_flag"], config["api_key"],
             {"deviceSN": config["device_sn"], "enable": enable},
             log, dry_run=dry_run)


# ---------------------------------------------------------------- commands

def cmd_get(args, config, log):
    api_post(SCHEDULER_API[args.api_version]["get"], config["api_key"],
             {"deviceSN": config["device_sn"]}, log, dry_run=args.dry_run)


def cmd_settings(args, config, log):
    for key in ("WorkMode", "MaxSoc", "MinSoc", "MinSocOnGrid", "ExportLimit"):
        try:
            api_post(PATH_SETTING_GET, config["api_key"],
                     {"deviceSN": config["device_sn"], "key": key},
                     log, dry_run=args.dry_run)
        except SystemExit:
            log.warn("could not read setting '{}' - continuing".format(key))
        time.sleep(1.1)  # query rate limit is 1/second


def cmd_set(args, config, log):
    version = args.api_version
    min_soc_on_grid = int(args.min_soc_on_grid
                          if args.min_soc_on_grid is not None
                          else config.get("min_soc_on_grid", MIN_SOC_ON_GRID_FLOOR))
    if not (MIN_SOC_ON_GRID_FLOOR <= min_soc_on_grid <= 100):
        raise SystemExit("minSocOnGrid must be between {} and 100, got {}".format(
            MIN_SOC_ON_GRID_FLOOR, min_soc_on_grid))
    if args.mode not in SCHEDULER_WORK_MODES:
        log.warn("'{}' is not in the scheduler workMode enumeration ({}) - the "
                 "inverter may reject it".format(
                     args.mode, ", ".join(SCHEDULER_WORK_MODES)))

    fd_pwr = args.fd_pwr
    if fd_pwr is None and config.get("fd_pwr"):
        fd_pwr = int(config["fd_pwr"])
    fd_soc, fd_pwr = resolve_force_params(
        work_mode=args.mode,
        max_soc=args.max_soc,
        min_soc_on_grid=min_soc_on_grid,
        fd_soc=args.fd_soc,
        fd_pwr=fd_pwr,
        log=log,
    )

    group = build_group(
        version=version,
        start=args.start,
        end=args.end,
        work_mode=args.mode,
        max_soc=args.max_soc,
        min_soc_on_grid=min_soc_on_grid,
        fd_soc=fd_soc,
        fd_pwr=fd_pwr,
    )
    log.info("setting single {} window {}-{} on {} maxSoc={} minSocOnGrid={} "
             "fdSoc={} fdPwr={}".format(args.mode, args.start, args.end, version,
                                        args.max_soc, min_soc_on_grid, fd_soc, fd_pwr))

    # The groups are useless while the master switch is off, so turn it on first.
    if not args.no_master_switch:
        set_master_switch(version, config, log, 1, args.dry_run)
        time.sleep(1.1)  # stay inside the ~1 request/second budget

    payload = {"deviceSN": config["device_sn"], "groups": [group]}
    if version == "v3":
        # false: leave extraParam fields we did not send alone.
        payload["isDefault"] = False
    api_post(SCHEDULER_API[version]["enable"], config["api_key"], payload,
             log, dry_run=args.dry_run)


def cmd_clear(args, config, log):
    version = args.api_version
    log.info("clearing all scheduler groups - inverter reverts to its work mode setting")
    api_post(SCHEDULER_API[version]["enable"], config["api_key"],
             {"deviceSN": config["device_sn"], "groups": []},
             log, dry_run=args.dry_run)
    # Turning the master switch off is what actually hands control back to the
    # inverter's own work mode setting.
    if not args.no_master_switch:
        time.sleep(1.1)
        set_master_switch(version, config, log, 0, args.dry_run)


def cmd_flag(args, config, log):
    version = args.api_version
    if args.enable is None:
        api_post(SCHEDULER_API[version]["get_flag"], config["api_key"],
                 {"deviceSN": config["device_sn"]}, log, dry_run=args.dry_run)
        return
    set_master_switch(version, config, log, args.enable, args.dry_run)


# ---------------------------------------------------------------- cli

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Control FoxESS Cloud inverter schedules.")
    parser.add_argument("--config", default=os.environ.get("FOXESS_CONFIG", DEFAULT_CONFIG),
                        help="path to config file (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="log the request but do not send it")
    parser.add_argument("--quiet", action="store_true",
                        help="log to file only, not stdout")
    parser.add_argument("--api-version", default=None,
                        choices=sorted(SCHEDULER_API),
                        help="scheduler API version (default: {}; v0 is "
                             "deprecated and ignores maxSoc)".format(DEFAULT_API_VERSION))
    parser.add_argument("--no-master-switch", action="store_true",
                        help="do not touch the scheduler master switch")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("get", help="read the current schedule")
    subparsers.add_parser("settings", help="read current work mode and SoC settings")
    subparsers.add_parser("clear", help="remove all schedule groups")

    flag_parser = subparsers.add_parser(
        "flag", help="read or set the scheduler master switch")
    flag_group = flag_parser.add_mutually_exclusive_group()
    flag_group.add_argument("--enable", dest="enable", action="store_const", const=1,
                            help="turn the scheduler on")
    flag_group.add_argument("--disable", dest="enable", action="store_const", const=0,
                            help="turn the scheduler off")
    flag_parser.set_defaults(enable=None)

    def add_window_args(sub, with_mode):
        sub.add_argument("--start", required=True, help="window start, HH:MM")
        sub.add_argument("--end", required=True, help="window end, HH:MM")
        if with_mode:
            sub.add_argument("--mode", default="ForceCharge", choices=WORK_MODES)
        sub.add_argument("--max-soc", type=int, default=100,
                         help="charge ceiling percent (default: %(default)s)")
        sub.add_argument("--min-soc-on-grid", type=int, default=None,
                         help="grid-present discharge floor percent (10-100)")
        sub.add_argument("--fd-soc", type=int, default=None,
                         help="SoC at which force charge/discharge stops "
                              "(default: maxSoc for ForceCharge, else minSocOnGrid)")
        sub.add_argument("--fd-pwr", type=int, default=None,
                         help="max power in watts for force charge/discharge; for "
                              "ForceCharge this caps grid import, and 0 disables it "
                              "(default: {})".format(DEFAULT_FD_PWR))

    add_window_args(subparsers.add_parser(
        "set", help="write a single-window schedule"), with_mode=True)
    # convenience alias matching the common case
    add_window_args(subparsers.add_parser(
        "force-charge", help="alias for: set --mode ForceCharge"), with_mode=False)

    args = parser.parse_args(argv)

    bootstrap_log = Log(DEFAULT_LOG, echo=not args.quiet)
    config = load_config(args.config, bootstrap_log)
    log = Log(config.get("log_file", DEFAULT_LOG),
              int(config.get("log_max_bytes", DEFAULT_LOG_MAX_BYTES)),
              echo=not args.quiet)
    log.info("device {} - credentials {}".format(
        config["device_sn"], config["_source"]))

    # CLI wins over the config file, which wins over the built-in default.
    if args.api_version is None:
        args.api_version = config.get("api_version", DEFAULT_API_VERSION)
    if args.api_version not in SCHEDULER_API:
        raise SystemExit("unknown api_version '{}' - choose one of {}".format(
            args.api_version, ", ".join(sorted(SCHEDULER_API))))
    if args.api_version == "v0":
        log.warn("scheduler v0 is deprecated and has no maxSoc field - "
                 "--max-soc will be ignored")

    if args.command == "force-charge":
        args.mode = "ForceCharge"
        args.command = "set"

    handlers = {
        "get": cmd_get,
        "settings": cmd_settings,
        "set": cmd_set,
        "clear": cmd_clear,
        "flag": cmd_flag,
    }
    handlers[args.command](args, config, log)


if __name__ == "__main__":
    main()
