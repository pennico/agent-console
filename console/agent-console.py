#!/usr/bin/env python3
"""Agent console: the web side and the action runner, in one file, two modes.

  serve   (in the agent-console container, unprivileged): the page, status.json,
          password unlock and 30-minute sessions; asks the runner to act.
  runner  (on the host, as the console user): runs the fixed allow-list of actions; listens
          only on a local Unix socket that the container mounts.
  passwd  set the console password (stores an scrypt hash, never the password).

A request may act only when all hold:
  - Tailscale-User-Login (set by Tailscale Serve for a user's device) is the
    owner's login (OWNER_LOGIN in /etc/agent-console/env);
  - it carries a live session cookie (HttpOnly, Secure, SameSite=Strict) from
    POST /api/unlock with the password (5 wrong tries lock unlocking for 10
    minutes), and the X-Console-Action header, which a page on another
    origin can't send without a CORS preflight that is never answered;
  - the runner accepts it: action from ACTIONS, target from a fixed set and,
    where the action takes one, a value matching its pattern. Commands are
    fixed argv lists, never a shell. One action at a time, each one logged.
Changes that need sudo go through the console terminal (term.py) instead.

Site specifics (container names, the ops repo, Infisical template names, cron
jobs, Overwatch tiers, timezone) come from the TOML config in
AGENT_CONSOLE_CONFIG (default /etc/agent-console/console.toml); see
examples/console.example.toml. Environment variables override paths.
"""

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

try:
    import tomllib
except ImportError:   # Python < 3.11
    tomllib = None


def _load_config():
    try:
        with open(os.environ.get("AGENT_CONSOLE_CONFIG", "/etc/agent-console/console.toml"), "rb") as fh:
            return tomllib.load(fh) if tomllib else {}
    except (OSError, ValueError):
        return {}


CFG = _load_config()


def cfg(section, key, default=None):
    node = CFG
    for part in section.split("."):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node.get(key, default) if isinstance(node, dict) else default


PORT = int(os.environ.get("CONSOLE_PORT", "8097"))
BIND = os.environ.get("CONSOLE_BIND", "127.0.0.1")   # 0.0.0.0 inside the container; the host publishes 127.0.0.1 only
RUNNER_SOCK = os.environ.get("CONSOLE_RUNNER_SOCK", cfg("console", "runner_sock", "/var/lib/agent-console/run/runner.sock"))
PASSWORD_FILE = os.environ.get("CONSOLE_PASSWORD_FILE", cfg("console", "password_file", "/etc/agent-console/password"))
COOKIE_SECURE = os.environ.get("CONSOLE_COOKIE_SECURE", "1") == "1"   # Tailscale Serve is https
COOKIE = "__Host-console" if COOKIE_SECURE else "console"
CONSOLE_ORIGIN = os.environ.get("CONSOLE_ORIGIN", "")   # e.g. https://<server>.<tailnet>.ts.net:8097
PASSKEYS_FILE = os.environ.get("CONSOLE_PASSKEYS", cfg("console", "passkeys", "/var/lib/agent-console/state/passkeys.json"))
RP_ID = os.environ.get("CONSOLE_RP_ID", "") or CONSOLE_ORIGIN.split("://", 1)[-1].split(":")[0].split("/")[0]
_challenges = {}        # challenge (b64url) -> (purpose, expiry); one use each
WEB = os.environ.get("CONSOLE_WEB", cfg("paths", "web", "/var/lib/agent-console/web"))
COLLECTOR = os.environ.get("CONSOLE_COLLECTOR", cfg("console", "collector", "/opt/agent-console/agent-status.py"))
OWNER_LOGIN = os.environ.get("OWNER_LOGIN", "")
LOG = os.environ.get("CONSOLE_LOG", cfg("console", "log", "/var/lib/agent-console/actions.log"))
_TEMPLATES = cfg("infisical", "templates", "/srv/infisical/templates")
JOBBOT_TEMPLATE = os.environ.get("JOBBOT_TEMPLATE", f"{_TEMPLATES}/{cfg('jobbot', 'template', 'job-bot')}.tmpl")
RUNNER_TEMPLATE = os.environ.get("RUNNER_TEMPLATE", f"{_TEMPLATES}/{cfg('runner', 'template', 'agent-runner')}.tmpl")
ROLES = ("impl", "arch", "qa", "adv", "verify", "overseer", "chat")   # agent_backend.py AGENT_BACKEND_<ROLE>
RUNNER_DIR = os.environ.get("AGENT_RUNNER_DIR", cfg("runner", "dir", "/srv/agent-runner"))
RUNNER_CT = cfg("runner", "container", "agent-runner")
JOBBOT_CT = cfg("jobbot", "container", "job-bot")
CLAUDE_CT = cfg("claude", "container", "claude-agent")
OPS_REPO = cfg("runner", "ops_repo", "ops")                        # runs the overseer
CT_PROJECTS = cfg("runner", "container_projects", "/app/projects")  # the projects folder inside the runner container
CT_SUPERVISOR = cfg("runner", "container_supervisor", "/app/supervisor.py")
CONTROL = CT_PROJECTS + "/.control"   # supervisor pause/stop flags, inside the runner container
# The console terminal (ttyd running term.py) for changes that need sudo: local port, and the
# Tailscale Serve port the page opens. term.py has its own allow-list; this only reports it's up.
TERM_PORT = int(os.environ.get("CONSOLE_TERM_PORT", "8098"))
TERM_PUBLIC = int(os.environ.get("CONSOLE_TERM_PUBLIC", str(TERM_PORT)))
AGENTS_MODE = os.path.expanduser(cfg("runner", "mode_command", "~/agents-mode"))
OVERWATCH_CALENDAR = "/usr/local/sbin/overwatch-calendar"
TZ = ZoneInfo(cfg("site", "timezone", "UTC"))
OVERWATCH_TIERS = tuple(cfg("overwatch", "tiers", ()))
OW_UNIT = cfg("overwatch", "unit_prefix", "overwatch-")
CONTAINERS = (RUNNER_CT, JOBBOT_CT, CLAUDE_CT)
CRON_JOBS = {k: v.get("match", k) for k, v in (CFG.get("cron") or {}).items() if isinstance(v, dict)}   # as in agent-status.py
RUN_WINDOW = cfg("jobbot", "run_window", None)   # ["06:45", "07:45"]: no manual run or restart next to the scheduled one
CRON_OFF = "#agent-console-off# "
MAIL_INTERVALS = ("0", "1800", "3600", "7200", "21600")
RESTART_INFISICAL = cfg("infisical", "restart", ["sudo", "-n", "/usr/bin/systemctl", "restart", "infisical-agent"])
_busy = threading.Lock()
SESSION_S = int(os.environ.get("CONSOLE_SESSION_S", "1800"))   # unlock lasts 30 minutes
_sessions = {}          # session token -> expiry (epoch); in memory, so a restart locks everyone
_fails = []             # times of wrong keys, for the 5-per-10-minutes limit
_sessions_lock = threading.Lock()

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CRON_EXPR = re.compile(r"^[0-9*/,-]+( [0-9*/,-]+){4}$")
CALENDAR = re.compile(r"^[0-9A-Za-z*,:./~ -]{3,80}$")


def run(argv, timeout=180, stdin=None):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin)
        return r.returncode, (r.stdout + r.stderr).strip()[-400:]
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)


def jobbot_window():
    """job-bot's own run is Mon-Fri at RUN_AT; a manual run or a restart next
    to it would cut into it. The window comes from the config (run_window)."""
    if not RUN_WINDOW:
        return False
    now = dt.datetime.now(TZ)
    a, b = (dt.time(*map(int, x.split(":"))) for x in RUN_WINDOW)
    return now.weekday() < 5 and a <= now.time() <= b


WINDOW_MSG = (f"job-bot is about to run or running ({RUN_WINDOW[0]}–{RUN_WINDOW[1]} on weekdays). Try after {RUN_WINDOW[1]}."
              if RUN_WINDOW else "")


def jobbot_once_argv():
    """The daemon's own command with --daemon swapped for --once, so the
    run uses exactly the interpreter and module the container runs."""
    code, out = run(["docker", "inspect", "--format", "{{json .Config.Cmd}}", JOBBOT_CT], 15)
    try:
        cmd = json.loads(out) if code == 0 else None
    except ValueError:
        cmd = None
    if not cmd or "bot.main" not in " ".join(cmd) or "--daemon" not in cmd:
        return None
    return ["docker", "exec", "-d", JOBBOT_CT] + ["--once" if a == "--daemon" else a for a in cmd]


def argv_action(build):
    def go(target, value):
        argv = build(target, value)
        return run(argv) if argv else (503, "This action isn't available on the server.")
    return go


def set_template_key(key, value, path=JOBBOT_TEMPLATE, app=JOBBOT_CT):
    """Set (or, with value None, remove) one plain KEY=value line in an
    Infisical template. The agent renders the app's .env from it (editing
    the .env itself would be overwritten) and reload.sh recreates the app;
    the same route agents-mode uses for AGENT_BACKEND. Only that line is
    touched; the file's other lines are never logged."""
    st = os.stat(path)
    with open(path) as fh:
        lines = fh.read().splitlines(keepends=True)
    pat = re.compile(rf"^\s*(export\s+)?{re.escape(key)}=")
    hits = [i for i, line in enumerate(lines) if pat.match(line)]
    if value is None:
        lines = [line for i, line in enumerate(lines) if i not in hits]
    elif hits:
        lines[hits[0]] = f"{key}={value}\n"
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as fh:
        fh.writelines(lines)
    os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, path)
    code, out = run(RESTART_INFISICAL, 60)
    if code != 0:
        return code, "Template saved, but infisical-agent did not restart: " + out
    return 0, f"Saved. The Infisical agent re-renders {app}'s .env and recreates {app} within about a minute."


def project_repos():
    """Projects the supervisor starts (its PROJECT_REPOS), read from its source on the host."""
    try:
        text = open(os.path.join(RUNNER_DIR, "supervisor.py")).read()
    except OSError:
        return ()
    m = re.search(r"^PROJECT_REPOS\s*=\s*\[([^\]]*)\]", text, re.M)
    return tuple(re.findall(r"[\"']([\w.-]+)[\"']", m.group(1))) if m else ()


def worker_flag(kind):
    """pause / resume / stop-session for one project: a flag file the
    supervisor reads every minute (docker exec, fixed paths, repo from the
    supervisor's own list)."""
    def go(target, value):
        if kind == "resume":
            return run(["docker", "exec", RUNNER_CT, "rm", "-f", f"{CONTROL}/pause-{target}"], 30)
        code, out = run(["docker", "exec", RUNNER_CT, "mkdir", "-p", CONTROL], 30)
        if code != 0:
            return code, out
        name = "pause" if kind == "pause" else "stop"
        code, out = run(["docker", "exec", RUNNER_CT, "touch", f"{CONTROL}/{name}-{target}"], 30)
        if code == 0:
            out = ("Paused. The current loop ends gently within a minute; work in progress is stashed." if kind == "pause"
                   else "Stop requested. The current session ends within a minute; its work is stashed and resumed on the next tick.")
        return code, out
    return go


def role_model(target, value):
    """Pin one dispatcher role to claude or vibe (AGENT_BACKEND_<ROLE>), or
    drop the pin so the role follows the backend mode again."""
    return set_template_key("AGENT_BACKEND_" + target.upper(), None if value == "default" else value,
                            path=RUNNER_TEMPLATE, app=RUNNER_CT)


def jobbot_setting(key):
    def go(target, value):
        if jobbot_window():
            return 409, WINDOW_MSG
        return set_template_key(key, value)
    return go


def cron_job(target, value):
    """Change, pause or resume one known line in the console user's own crontab."""
    code, tab = run(["crontab", "-l"], 15)
    if code != 0:
        return code, "Could not read the crontab."
    lines, found = tab.splitlines(), False
    for i, line in enumerate(lines):
        if CRON_JOBS[target] not in line:
            continue
        found = True
        off = line.startswith(CRON_OFF)
        body = line[len(CRON_OFF):] if off else line
        if body.lstrip().startswith("#"):
            return 409, "That cron line is commented out by hand; change it over SSH."
        if value == "off":
            lines[i] = CRON_OFF + body
        elif value == "on":
            lines[i] = body
        else:
            fields = body.split(None, 5)
            if len(fields) < 6:
                return 409, "That cron line has an unexpected format; change it over SSH."
            lines[i] = (CRON_OFF if off else "") + value + " " + fields[5]
        break
    if not found:
        return 404, f"No line for {CRON_JOBS[target]} in the crontab."
    return run(["crontab", "-"], 15, stdin="\n".join(lines) + "\n")


ACTIONS = {
    "restart": {"targets": CONTAINERS,
                "run": argv_action(lambda t, v: ["docker", "restart", "--time", "60", t])},
    "stop": {"targets": CONTAINERS,
             "run": argv_action(lambda t, v: ["docker", "stop", "--time", "60", t])},
    "start": {"targets": CONTAINERS,
              "run": argv_action(lambda t, v: ["docker", "start", t])},
    "overseer-now": {"targets": (OPS_REPO,),
                     "run": argv_action(lambda t, v: ["docker", "exec", "-d", "-w", f"{CT_PROJECTS}/{OPS_REPO}",
                                                      RUNNER_CT, "bash", "dispatcher/run-overseer.sh"])},
    "jobbot-run-now": {"targets": (JOBBOT_CT,), "run": argv_action(lambda t, v: jobbot_once_argv())},
    "jobbot-time": {"targets": (JOBBOT_CT,), "value": HHMM.fullmatch, "run": jobbot_setting("RUN_AT")},
    "jobbot-mail": {"targets": (JOBBOT_CT,), "value": lambda v: v in MAIL_INTERVALS,
                    "run": jobbot_setting("EMAIL_CHECK_INTERVAL_S")},
    "infisical-rerender": {"targets": ("all",), "run": argv_action(lambda t, v: RESTART_INFISICAL)},
    "pause": {"targets": project_repos, "run": worker_flag("pause")},
    "resume": {"targets": project_repos, "run": worker_flag("resume")},
    "stop-session": {"targets": project_repos, "run": worker_flag("stop-session")},
    "role-model": {"targets": ROLES, "value": lambda v: v in ("default", "claude", "vibe"), "run": role_model},
    "backend": {"targets": ("claude", "vibe", "both"),
                "run": argv_action(lambda t, v: [AGENTS_MODE, t])},
    "cron": {"targets": tuple(CRON_JOBS), "value": lambda v: v in ("on", "off") or CRON_EXPR.fullmatch(v),
             "run": cron_job},
    "overwatch-run": {"targets": OVERWATCH_TIERS,
                      "run": argv_action(lambda t, v: ["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block",
                                                       f"{OW_UNIT}{t}.service"])},
    "overwatch-timer": {"targets": OVERWATCH_TIERS, "value": lambda v: v in ("on", "off"),
                        "run": argv_action(lambda t, v: ["sudo", "-n", "/usr/bin/systemctl",
                                                         "enable" if v == "on" else "disable", "--now",
                                                         f"{OW_UNIT}{t}.timer"])},
    "overwatch-calendar": {"targets": OVERWATCH_TIERS, "value": CALENDAR.fullmatch,
                           "run": argv_action(lambda t, v: ["sudo", "-n", OVERWATCH_CALENDAR, t, v])},
}



def term_up():
    import socket
    try:
        with socket.create_connection(("127.0.0.1", TERM_PORT), timeout=1):
            return True
    except OSError:
        return False


def sudo_ok(*argv):
    return run(["sudo", "-n", "-l", *argv], 10)[0] == 0


def available():
    crontab = run(["crontab", "-l"], 10)[1]
    restart_ok = sudo_ok(*RESTART_INFISICAL[2:])
    template_ok = os.access(JOBBOT_TEMPLATE, os.W_OK) and restart_ok
    return {
        "restart": True, "stop": True, "start": True,
        "overseer-now": True,
        "jobbot-run-now": jobbot_once_argv() is not None,
        "jobbot-time": template_ok, "jobbot-mail": template_ok,
        "role-model": os.access(RUNNER_TEMPLATE, os.W_OK) and restart_ok,
        "infisical-rerender": restart_ok,
        # only once agent-runner runs a supervisor that reads the flags
        **dict.fromkeys(("pause", "resume", "stop-session"),
                        run(["docker", "exec", RUNNER_CT, "grep", "-q", "CONTROL_DIR", CT_SUPERVISOR], 15)[0] == 0),
        "backend": os.access(AGENTS_MODE, os.X_OK),
        "cron": any(n in crontab for n in CRON_JOBS.values()),
        # one-click only with the optional sudoers rules; otherwise the page uses the console terminal
        **({"overwatch-run": sudo_ok("/usr/bin/systemctl", "start", "--no-block", f"{OW_UNIT}{OVERWATCH_TIERS[0]}.service"),
            "overwatch-timer": sudo_ok("/usr/bin/systemctl", "disable", "--now", f"{OW_UNIT}{OVERWATCH_TIERS[0]}.timer"),
            "overwatch-calendar": sudo_ok(OVERWATCH_CALENDAR, OVERWATCH_TIERS[0], "07:00")} if OVERWATCH_TIERS else {}),
    }


def audit(entry):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def refresh_status():
    run(["python3", COLLECTOR], 60)


def now_iso():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


# ---- runner (host, the console user): executes the allow-list; listens on a local socket only ----

def perform(name, target, value, login):
    """Validate and run one action. Returns (http status, body). This is the
    trust boundary: the web side's checks are repeated here."""
    spec = ACTIONS.get(name)
    targets = spec and (spec["targets"]() if callable(spec["targets"]) else spec["targets"])
    if not spec or target not in targets:
        return 400, {"error": f"Unknown action {name!r} / target {target!r}."}
    check = spec.get("value")
    if (check is None and value) or (check is not None and not check(value)):
        return 400, {"error": f"Value {value!r} isn't valid for {name}."}
    if name == "jobbot-run-now" and jobbot_window():
        return 409, {"error": WINDOW_MSG}
    if not _busy.acquire(blocking=False):
        return 409, {"error": "Another action is still running. Try again in a moment."}
    try:
        started = time.time()
        code, out = spec["run"](target, value)
        took = round(time.time() - started, 1)
        audit({"at": now_iso(), "login": login, "action": name, "target": target,
               "value": value or None, "exit": code, "took_s": took})
    except OSError as e:
        code, out, took = 500, str(e), 0
    finally:
        _busy.release()
    threading.Thread(target=refresh_status, daemon=True).start()
    if code in (404, 409, 503):
        return code, {"error": out}
    if code != 0:
        return 500, {"error": f"Command failed (exit {code}).", "output": out}
    return 200, {"ok": True, "took_s": took, "output": out}


class RunnerHandler(socketserver.StreamRequestHandler):
    """One JSON request per connection: {"op": "ping"} | {"op": "action", ...} | {"op": "audit", "entry": {...}}."""

    def handle(self):
        try:
            req = json.loads(self.rfile.readline(8192) or b"{}")
            op = req.get("op")
            if op == "ping":
                res = {"actions": available(), "term": {"port": TERM_PUBLIC} if term_up() else None}
            elif op == "action":
                code, body = perform(str(req.get("action", "")), str(req.get("target", "")),
                                     str(req.get("value", "")).strip(), str(req.get("login", "")))
                res = {"status": code, "body": body}
            elif op == "audit" and isinstance(req.get("entry"), dict):
                audit({**{k: v for k, v in req["entry"].items() if k in ("action", "exit", "login")}, "at": now_iso(), "via": "web"})
                res = {"ok": True}
            else:
                res = {"error": "unknown op"}
        except (ValueError, AttributeError) as e:
            res = {"error": f"bad request: {e}"}
        self.wfile.write(json.dumps(res).encode() + b"\n")


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def run_runner():
    try:
        os.unlink(RUNNER_SOCK)
    except FileNotFoundError:
        pass
    old = os.umask(0o077)          # socket owner-only: the console user (the container runs as the same uid)
    server = UnixServer(RUNNER_SOCK, RunnerHandler)
    os.umask(old)
    server.serve_forever()


def runner_call(req, timeout=240):
    """Web side → runner. Raises OSError if the runner is down."""
    import socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(RUNNER_SOCK)
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b"{}")


# ---- password: scrypt hash in PASSWORD_FILE, set with `agent-console.py passwd` ----

def hash_password(pw):
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 15, 8, 1
    h = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def check_password(pw):
    try:
        with open(PASSWORD_FILE) as fh:
            kind, n, r, p, salt, h = fh.read().strip().split("$")
    except (OSError, ValueError):
        return False
    if kind != "scrypt":
        return False
    got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                         maxmem=64 * 1024 * 1024, dklen=len(h) // 2)
    return hmac.compare_digest(got.hex(), h)


def set_password(path):
    import getpass
    pw = getpass.getpass("New console password (16+ characters, or 4-5 random words): ")
    if len(pw) < 12:
        raise SystemExit("Too short: use at least 12 characters (16+ recommended).")
    if getpass.getpass("Again: ") != pw:
        raise SystemExit("The two entries differ; nothing changed.")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
    with os.fdopen(fd, "w") as fh:
        fh.write(hash_password(pw) + "\n")
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
    print(f"Saved the hash to {path}. Restart the console container to end open sessions.")


# ---- passkeys (Touch ID / Face ID): WebAuthn with ES256, as a second way to unlock ----
# Public keys only, in PASSKEYS_FILE. The signature check uses the `cryptography` package
# (installed in the image); without it passkeys are simply unavailable.

def b64u(data):
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text):
    import base64
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def cbor(data, i=0):
    """Minimal CBOR decoder for WebAuthn: returns (value, next index)."""
    ib = data[i]; major, info = ib >> 5, ib & 31; i += 1
    if info < 24:
        n = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        n = int.from_bytes(data[i:i + size], "big"); i += size
    else:
        raise ValueError("indefinite CBOR not supported")
    if major == 0:
        return n, i
    if major == 1:
        return -1 - n, i
    if major == 2:
        return data[i:i + n], i + n
    if major == 3:
        return data[i:i + n].decode(), i + n
    if major == 4:
        out = []
        for _ in range(n):
            v, i = cbor(data, i); out.append(v)
        return out, i
    if major == 5:
        out = {}
        for _ in range(n):
            k, i = cbor(data, i); v, i = cbor(data, i); out[k] = v
        return out, i
    if major == 7:
        return {20: False, 21: True, 22: None}.get(info), i
    raise ValueError("unsupported CBOR type")


def passkeys_available():
    try:
        import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
        return bool(RP_ID and CONSOLE_ORIGIN)
    except ImportError:
        return False


def load_passkeys():
    try:
        with open(PASSKEYS_FILE) as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_passkeys(keys):
    os.makedirs(os.path.dirname(PASSKEYS_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PASSKEYS_FILE))
    with os.fdopen(fd, "w") as fh:
        json.dump(keys, fh, indent=1)
    os.replace(tmp, PASSKEYS_FILE)


def new_challenge(purpose):
    now = time.time()
    c = b64u(secrets.token_bytes(32))
    with _sessions_lock:
        for k in [k for k, (_, exp) in _challenges.items() if exp < now]:
            del _challenges[k]
        _challenges[c] = (purpose, now + 120)
    return c


def check_client_data(client_data_json, kind, purpose):
    cd = json.loads(client_data_json)
    if cd.get("type") != kind:
        raise ValueError("wrong ceremony type")
    with _sessions_lock:
        got = _challenges.pop(cd.get("challenge", ""), None)   # one use, even when the rest fails
    if not got or got[0] != purpose or got[1] < time.time():
        raise ValueError("unknown or expired challenge")
    if cd.get("origin") != CONSOLE_ORIGIN or cd.get("crossOrigin"):
        raise ValueError("wrong origin")


def parse_auth_data(ad):
    if len(ad) < 37:
        raise ValueError("short authenticator data")
    if ad[:32] != hashlib.sha256(RP_ID.encode()).digest():
        raise ValueError("wrong relying party")
    flags, count = ad[32], int.from_bytes(ad[33:37], "big")
    if not flags & 0x01 or not flags & 0x04:
        raise ValueError("user presence and verification (Touch ID / Face ID) are required")
    cred = None
    if flags & 0x40:   # attested credential data (registration)
        n = int.from_bytes(ad[53:55], "big")
        cred_id = ad[55:55 + n]
        key, _ = cbor(ad, 55 + n)
        cred = (cred_id, key)
    return count, cred


def register_passkey(req, name):
    client_data = unb64u(req.get("clientDataJSON", ""))
    check_client_data(client_data, "webauthn.create", "register")
    att, _ = cbor(unb64u(req.get("attestationObject", "")))
    count, cred = parse_auth_data(att["authData"])
    if not cred:
        raise ValueError("no credential in the response")
    cred_id, key = cred
    if key.get(1) != 2 or key.get(3) != -7 or key.get(-1) != 1:
        raise ValueError("only ES256 (P-256) passkeys are supported")
    keys = load_passkeys()
    if any(k["id"] == b64u(cred_id) for k in keys):
        raise ValueError("this passkey is already registered")
    keys.append({"id": b64u(cred_id), "x": key[-2].hex(), "y": key[-3].hex(), "sign_count": count,
                 "name": (str(name).strip() or "passkey")[:40], "created": time.time(), "last_used": None})
    save_passkeys(keys)
    return keys[-1]


def login_passkey(req):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    keys = load_passkeys()
    k = next((k for k in keys if k["id"] == req.get("id")), None)
    if not k:
        raise ValueError("unknown passkey")
    client_data = unb64u(req.get("clientDataJSON", ""))
    check_client_data(client_data, "webauthn.get", "login")
    ad = unb64u(req.get("authenticatorData", ""))
    count, _ = parse_auth_data(ad)
    pub = ec.EllipticCurvePublicNumbers(int(k["x"], 16), int(k["y"], 16), ec.SECP256R1()).public_key()
    try:
        pub.verify(unb64u(req.get("signature", "")), ad + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise ValueError("bad signature") from None
    if count and k["sign_count"] and count <= k["sign_count"]:
        raise ValueError("signature counter went backwards (cloned key?)")
    k["sign_count"], k["last_used"] = count, time.time()
    save_passkeys(keys)
    return k


# ---- web side (container): page, status, unlock and sessions; asks the runner to act ----

class Handler(BaseHTTPRequestHandler):
    server_version = "agent-console"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # actions go to the audit log; page polls are noise

    def send(self, code, body, ctype="application/json", cache=False, cookie=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def owner(self):
        login = self.headers.get("Tailscale-User-Login", "")
        return bool(OWNER_LOGIN) and hmac.compare_digest(login, OWNER_LOGIN)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self.static("index.html", "text/html; charset=utf-8")
        if path in ("/status.json", "/ledger.json"):
            return self.static(path[1:], "application/json")
        if path == "/api/ping":
            if not self.owner():
                return self.send(403, {"error": "Only the owner's Tailscale login can use actions."})
            try:
                res = runner_call({"op": "ping"}, 60)
            except OSError:   # keep unlocking and passkeys usable; every action shows as unavailable
                res = {"actions": {}, "term": None, "runner": "down"}
            keys = load_passkeys()
            unlocked = self.session_until()
            return self.send(200, {**res, "unlocked_until": unlocked, "passkeys": {
                "available": passkeys_available(), "count": len(keys),
                "list": [{k2: k[k2] for k2 in ("id", "name", "created", "last_used")} for k in keys] if unlocked else []}})
        self.send(404, {"error": "not found"})

    def static(self, name, ctype):
        try:
            with open(os.path.join(WEB, name), "rb") as fh:
                self.send(200, fh.read(), ctype)
        except OSError:
            self.send(404, {"error": f"{name} missing"})

    def body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        req = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
        if not isinstance(req, dict):
            raise ValueError("not an object")
        return req

    def session_id(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def session_until(self):
        s = self.session_id()
        with _sessions_lock:
            exp = _sessions.get(s)
        return exp if s and exp and exp > time.time() else None

    def cookie(self, value, max_age):
        flags = "; Secure" if COOKIE_SECURE else ""
        return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{flags}"

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/action", "/api/unlock", "/api/lock", "/api/passkey/options", "/api/passkey/register",
                        "/api/passkey/login", "/api/passkey/delete"):
            return self.send(404, {"error": "not found"})
        if not self.owner():
            return self.send(403, {"error": "Only the owner's Tailscale login can use actions."})
        # Defense in depth (the custom header below and the SameSite cookie already stop
        # cross-site requests): the Origin must be this console, as the proxy may rewrite Host.
        origin = self.headers.get("Origin")
        allowed = {self.headers.get("Host", ""), self.headers.get("X-Forwarded-Host", ""), CONSOLE_ORIGIN.split("://", 1)[-1]}
        if origin and origin.split("://", 1)[-1] not in allowed - {""}:
            return self.send(403, {"error": "Cross-origin request refused."})
        # A custom header can't be sent cross-site without a CORS preflight, which this server never answers.
        if self.headers.get("X-Console-Action") != "1":
            return self.send(403, {"error": "Missing X-Console-Action header."})
        try:
            req = self.body()
        except (ValueError, AttributeError):
            return self.send(400, {"error": "Body must be a JSON object."})
        if path == "/api/unlock":
            return self.unlock(req)
        if path.startswith("/api/passkey/"):
            return self.passkey(path.rsplit("/", 1)[1], req)
        if path == "/api/lock":
            with _sessions_lock:
                _sessions.pop(self.session_id(), None)
            return self.send(200, {"ok": True}, cookie=self.cookie("", 0))
        if not self.session_until():
            return self.send(401, {"error": "Locked: unlock with your password first (sessions last 30 minutes)."})
        try:
            res = runner_call({"op": "action", "action": str(req.get("action", "")), "target": str(req.get("target", "")),
                               "value": str(req.get("value", "")), "login": self.headers.get("Tailscale-User-Login", "")})
        except OSError:
            return self.send(503, {"error": "The action runner on the host isn't running."})
        self.send(int(res.get("status", 500)), res.get("body", {"error": "runner error"}))

    def passkey(self, op, req):
        if not passkeys_available():
            return self.send(503, {"error": "Passkeys aren't available on this console."})
        login = self.headers.get("Tailscale-User-Login", "")
        if op == "options":
            purpose = "register" if req.get("purpose") == "register" else "login"
            if purpose == "register" and not self.session_until():
                return self.send(401, {"error": "Unlock with your password first to add Touch ID / Face ID."})
            keys = load_passkeys()
            pk = {"challenge": new_challenge(purpose), "timeout": 60000, "rpId": RP_ID, "userVerification": "required"}
            if purpose == "register":
                pk.update(rp={"id": RP_ID, "name": "Agent console"},
                          user={"id": b64u(hashlib.sha256(login.encode()).digest()[:16]), "name": login, "displayName": login},
                          pubKeyCredParams=[{"type": "public-key", "alg": -7}], attestation="none",
                          excludeCredentials=[{"type": "public-key", "id": k["id"]} for k in keys],
                          authenticatorSelection={"residentKey": "preferred", "userVerification": "required"})
            else:
                pk["allowCredentials"] = [{"type": "public-key", "id": k["id"]} for k in keys]
            return self.send(200, {"publicKey": pk})
        if op == "register":
            if not self.session_until():
                return self.send(401, {"error": "Locked: unlock with your password first."})
            try:
                k = register_passkey(req, req.get("name", ""))
            except (ValueError, KeyError, IndexError, TypeError) as e:
                return self.send(400, {"error": f"Couldn't add the passkey: {e}."})
            self.audit_web("passkey-add", 0, k["name"])
            return self.send(200, {"ok": True, "name": k["name"]})
        if op == "delete":
            if not self.session_until():
                return self.send(401, {"error": "Locked: unlock with your password first."})
            keys = load_passkeys()
            left = [k for k in keys if k["id"] != req.get("id")]
            save_passkeys(left)
            self.audit_web("passkey-remove", 0 if len(left) < len(keys) else 1, "")
            return self.send(200, {"ok": True, "count": len(left)})
        if op == "login":
            now = time.time()
            with _sessions_lock:
                _fails[:] = [t for t in _fails if now - t < 600]
                if len(_fails) >= 5:
                    return self.send(429, {"error": "Too many failed tries. Try again in 10 minutes."})
            try:
                k = login_passkey(req)
            except (ValueError, KeyError, TypeError) as e:
                with _sessions_lock:
                    _fails.append(now)
                self.audit_web("unlock-passkey", 1, "")
                return self.send(401, {"error": f"Touch ID / Face ID didn't verify: {e}."})
            self.audit_web("unlock-passkey", 0, k["name"])
            return self.start_session()
        return self.send(404, {"error": "not found"})

    def audit_web(self, action, code, detail):
        try:
            runner_call({"op": "audit", "entry": {"action": action + (f" ({detail})" if detail else ""), "exit": code,
                                                  "login": self.headers.get("Tailscale-User-Login", "")}}, 10)
        except OSError:
            pass

    def start_session(self):
        now = time.time()
        s = secrets.token_urlsafe(32)
        with _sessions_lock:
            for k in [k for k, exp in _sessions.items() if exp < now]:
                del _sessions[k]
            _sessions[s] = now + SESSION_S
        self.send(200, {"ok": True, "expires_at": now + SESSION_S}, cookie=self.cookie(s, SESSION_S))

    def unlock(self, req):
        """Trade the password for a session cookie that expires after SESSION_S."""
        now = time.time()
        with _sessions_lock:
            _fails[:] = [t for t in _fails if now - t < 600]
            if len(_fails) >= 5:
                return self.send(429, {"error": "Too many wrong passwords. Try again in 10 minutes."})
        ok = check_password(str(req.get("password", "")))
        try:
            runner_call({"op": "audit", "entry": {"action": "unlock", "exit": 0 if ok else 1,
                                                  "login": self.headers.get("Tailscale-User-Login", "")}}, 10)
        except OSError:
            pass
        if not ok:
            with _sessions_lock:
                _fails.append(now)
            return self.send(401, {"error": "Wrong password."})
        self.start_session()


def main(argv):
    mode = argv[0] if argv else ""
    if mode == "runner":
        return run_runner()
    if mode == "passwd":
        return set_password(argv[1] if len(argv) > 1 else PASSWORD_FILE)
    if mode == "serve":
        if not OWNER_LOGIN:
            raise SystemExit("OWNER_LOGIN is not set (see /etc/agent-console/env)")
        return ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
    raise SystemExit("usage: agent-console.py serve | runner | passwd [file]")


if __name__ == "__main__":
    main(sys.argv[1:])
