#!/usr/bin/env python3
"""Agent console terminal: runs one allow-listed change that needs sudo.

ttyd starts this script (never a shell) with the URL's ?arg= values as
arguments: <action> <target> [<value>]. The script checks them against the
same patterns as the console service, prints every command it will run,
asks for confirmation, and lets sudo ask for the owner's password in the
terminal. The password is typed only here and never stored. Anything else
is refused.

Besides the built-in Overwatch actions, the site's config can define its own
commands under [terminal.commands.<name>] (action "run", target <name>):
fixed argv lists only, never a shell or a value from the URL. A command may
have a read-only `preview` that runs before the y/N question (to show what
would change) and `notes` printed with the plan. The config file is owned by
root, so only the owner can add a command.

  ttyd --interface lo --port 8098 --writable --url-arg ... python3 term.py

Overwatch tiers and the unit prefix come from the same TOML config as the
console (AGENT_CONSOLE_CONFIG, section [overwatch]).
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys

try:
    import tomllib
except ImportError:
    tomllib = None


def _load_config():
    try:
        with open(os.environ.get("AGENT_CONSOLE_CONFIG", "/etc/agent-console/console.toml"), "rb") as fh:
            return tomllib.load(fh) if tomllib else {}
    except (OSError, ValueError):
        return {}


CFG = _load_config()

LOG = os.environ.get("CONSOLE_LOG", CFG.get("console", {}).get("log", "/var/lib/agent-console/actions.log"))
COLLECTOR = os.environ.get("CONSOLE_COLLECTOR", CFG.get("console", {}).get("collector", "/opt/agent-console/agent-status.py"))
TIERS = tuple(CFG.get("overwatch", {}).get("tiers", ()))
UNIT = CFG.get("overwatch", {}).get("unit_prefix", "overwatch-")
CALENDAR = re.compile(r"[0-9A-Za-z*,:./~ -]{3,80}")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
BOLD, DIM, GREEN, RED, OFF = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"


def overwatch_calendar(tier, cal):
    """Drop-in that replaces the timer's OnCalendar (the unit file stays untouched)."""
    d = f"/etc/systemd/system/{UNIT}{tier}.timer.d"
    conf = f"[Timer]\nOnCalendar=\nOnCalendar={cal}\n"
    return [
        (["systemd-analyze", "calendar", cal], None, "check the calendar and show the next runs"),
        (["sudo", "install", "-d", "-m", "755", d], None, "create the drop-in folder"),
        (["sudo", "tee", f"{d}/agent-console.conf"], conf, "write the new OnCalendar"),
        (["sudo", "systemctl", "daemon-reload"], None, "reload systemd"),
        (["sudo", "systemctl", "restart", f"{UNIT}{tier}.timer"], None, "restart the timer"),
        (["systemctl", "list-timers", f"{UNIT}{tier}.timer", "--no-pager"], None, "show the next run"),
    ]


# action -> (allowed targets, value check or None, steps(target, value), what it does)
ACTIONS = {
    "overwatch-run": (TIERS, None,
                      lambda t, v: [(["sudo", "systemctl", "start", "--no-block", f"{UNIT}{t}.service"], None, "start the review now")],
                      "Run the Overwatch {t} review now"),
    "overwatch-timer": (TIERS, lambda v: v in ("on", "off"),
                        lambda t, v: [(["sudo", "systemctl", "enable" if v == "on" else "disable", "--now", f"{UNIT}{t}.timer"], None,
                                       "turn the timer " + v),
                                      (["systemctl", "list-timers", f"{UNIT}{t}.timer", "--all", "--no-pager"], None, "show its state")],
                        "Turn the Overwatch {t} timer {v}"),
    "overwatch-calendar": (TIERS, CALENDAR.fullmatch, overwatch_calendar, "Set the Overwatch {t} calendar to: {v}"),
    "overwatch-accept": (("all",), None,
                         lambda t, v: [(["sudo", "overwatch", "accept"], None, "accept the current security state as intended")],
                         "Accept the current security state in Overwatch"),
}


def _is_argv(xs):
    return isinstance(xs, list) and bool(xs) and all(isinstance(x, str) and x for x in xs)


def _site_commands(cfg):
    """[terminal.commands.<name>] -> {name: {what, steps, preview, notes}}; malformed entries are skipped."""
    out = {}
    for name, c in (cfg.get("terminal", {}).get("commands", {}) or {}).items():
        if not (NAME.fullmatch(name) and isinstance(c, dict)):
            continue
        steps = c.get("steps") or []
        if not (isinstance(steps, list) and steps and all(isinstance(st, dict) and _is_argv(st.get("argv")) for st in steps)):
            continue
        preview = c.get("preview")
        if preview is not None and not _is_argv(preview):
            continue
        out[name] = {
            "what": str(c.get("what") or f"Run {name}"),
            "steps": [(list(st["argv"]), None, str(st.get("why") or "")) for st in steps],
            "preview": list(preview) if preview else None,
            "notes": [str(n) for n in (c.get("notes") or []) if isinstance(n, (str, int, float))],
        }
    return out


COMMANDS = _site_commands(CFG)
ACTIONS["run"] = (tuple(COMMANDS), None, lambda t, v: COMMANDS[t]["steps"], "{t}")


def log(entry):
    try:
        with open(LOG, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def quote(argv):
    return " ".join(a if re.fullmatch(r"[\w@%+=:,./-]+", a) else "'" + a.replace("'", "'\\''") + "'" for a in argv)


def finish(code):
    print(f"\n{DIM}Press Enter to close this terminal.{OFF}", end="", flush=True)
    try:
        input()
    except EOFError:
        pass
    sys.exit(code)


def main(argv):
    print(f"{BOLD}Agent console · change that needs sudo{OFF}\n")
    if len(argv) < 2 or argv[0] not in ACTIONS:
        print(f"{RED}Unknown or missing action. Open this terminal from a console button.{OFF}")
        finish(2)
    action, target, value = argv[0], argv[1], argv[2] if len(argv) > 2 else ""
    targets, check, steps_of, what = ACTIONS[action]
    if target not in targets or (check is None and value) or (check is not None and not check(value)):
        print(f"{RED}Refused: {action} {target!r} {value!r} doesn't match what this action allows.{OFF}")
        finish(2)
    steps = steps_of(target, value)
    site = COMMANDS.get(target) if action == "run" else None
    print(f"{BOLD}{(site['what'] if site else what.format(t=target, v=value))}{OFF}\n")
    if site and site["preview"]:
        print(f"{DIM}$ {quote(site['preview'])}{OFF}", flush=True)
        r = subprocess.run(site["preview"])
        if r.returncode != 0:
            print(f"{RED}The preview failed (exit {r.returncode}); nothing was run.{OFF}")
            finish(r.returncode)
        print()
    for note in (site["notes"] if site else []):
        print(f"  • {note}")
    if site and site["notes"]:
        print()
    print("This will run:")
    for cmd, stdin, why in steps:
        print(f"  {DIM}# {why}{OFF}\n  {quote(cmd)}" + (f"   {DIM}(input: {stdin.strip()!r}){OFF}" if stdin else ""))
    try:
        answer = input("\nRun these now? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("Nothing changed.")
        finish(1)
    code = 0
    for cmd, stdin, _why in steps:
        print(f"\n{DIM}$ {quote(cmd)}{OFF}", flush=True)
        r = subprocess.run(cmd, input=stdin, text=True, stdout=None if stdin is None else subprocess.DEVNULL)
        if r.returncode != 0:
            code = r.returncode
            print(f"{RED}Failed (exit {code}); stopped here.{OFF}")
            break
    log({"at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "via": "terminal",
         "action": action, "target": target, "value": value or None, "exit": code})
    if code == 0:
        print(f"\n{GREEN}Done.{OFF} The console shows the change within a minute.")
        subprocess.run(["python3", COLLECTOR], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finish(code)


if __name__ == "__main__":
    main(sys.argv[1:])
