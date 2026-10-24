#!/usr/bin/env python3
"""Agent console collector: writes a read-only status snapshot of every agent
on the server to <web>/status.json (and ledger.json) for the console page.

Runs from cron every minute as the console's user. Read-only everywhere:
docker inspect / top / logs, the agent loop's project files and chat
transcripts, systemctl show, Infisical templates (key names only). Writes
only the web folder. Never reads or prints secret values: from .env files it
reads a few named, non-secret keys and nothing else.

Site specifics (paths, container names, projects, timers, cron jobs) come from
the TOML config in AGENT_CONSOLE_CONFIG (default /etc/agent-console/console.toml);
see examples/console.example.toml. The defaults below are generic.
"""

import datetime as dt
import glob
import gzip
import json
import os
import re
import subprocess
import time
import tomllib
from zoneinfo import ZoneInfo


def _load_config():
    try:
        with open(os.environ.get("AGENT_CONSOLE_CONFIG", "/etc/agent-console/console.toml"), "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


CFG = _load_config()


def cfg(section, key, default=None):
    """One value from the site config; `section` may be dotted ("runner.activity")."""
    node = CFG
    for part in section.split("."):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node.get(key, default) if isinstance(node, dict) else default


OUT = os.environ.get("AGENT_CONSOLE_OUT", cfg("paths", "web", "/var/lib/agent-console/web"))
RUNNER = os.environ.get("AGENT_RUNNER_DIR", cfg("runner", "dir", "/srv/agent-runner"))
PROJECTS = os.environ.get("AGENT_PROJECTS_DIR", cfg("runner", "projects", RUNNER + "/projects"))
RUNNER_CT = cfg("runner", "container", "agent-runner")
OPS_REPO = cfg("runner", "ops_repo", "ops")          # the project that runs the overseer
JOBBOT_CT = cfg("jobbot", "container", "job-bot")
JOBBOT_ENV = os.environ.get("JOBBOT_ENV", cfg("jobbot", "env", "/srv/job-bot/.env"))
CLAUDE_CT = cfg("claude", "container", "claude-agent")
# Host cron jobs the console shows and can change: id -> text that identifies the line.
CRON_JOBS = {k: v.get("match", k) for k, v in (CFG.get("cron") or {}).items() if isinstance(v, dict)}
CRON_OFF = "#agent-console-off# "
CHATS = os.environ.get("CLAUDE_CHATS_DIR", os.path.expanduser(cfg("claude", "chats", "~/.claude/projects/-workspace")))
REPOS_FALLBACK = cfg("runner", "projects_fallback", [])
# Evidence of work that runs without an agent session (run jobs, host jobs): newest mtime wins.
ACTIVITY = {repo: [tuple(x) for x in items] for repo, items in (cfg("runner", "activity", {}) or {}).items()}
APP_DECOMMISSIONED = set(cfg("runner", "app_decommissioned", []))   # app retired; project and pipelines may still run
LEDGER = os.path.join(OUT, "ledger.json")
INFISICAL_TEMPLATES = os.environ.get("INFISICAL_TEMPLATES", cfg("infisical", "templates", "/srv/infisical/templates"))
INFISICAL_AGENT_YAML = os.path.expanduser(cfg("infisical", "agent_yaml", "~/.config/infisical-agent/agent.yaml"))
CATALOG = os.environ.get("AGENT_CONSOLE_CATALOG", cfg("site", "catalog", "/etc/agent-console/catalog.json"))
# Settings whose values may be shown; every other plain line is reported by name only
# (some, like NTFY_TOPIC, are unguessable names that act as secrets).
SHOW_VALUES = {"AGENT_BACKEND", "RUN_AT", "EMAIL_CHECK_INTERVAL_S", "DISPATCH_MAX_PRICE", "DISPATCH_DAILY_SESSIONS",
               "DISPATCH_MAX_TURNS_REVIEW", "PREPARE_FALLBACK", "CLAUDE_BACKEND", "TZ",
               *(f"AGENT_BACKEND_{r.upper()}" for r in ("impl", "arch", "qa", "adv", "verify", "overseer", "chat")),
               *cfg("infisical", "show_values", [])}
SECRET_REF = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=.*getSecretByName\s+"[^"]*"\s+"[^"]*"\s+"/?([^"]*)"\s+"([^"]+)"')
PLAIN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
MISSING_AFTER_S = 120                  # gone for two collector runs before it counts as removed
OVERWATCH_TIERS = cfg("overwatch", "tiers", [])
OW_UNIT = cfg("overwatch", "unit_prefix", "overwatch-")
NOW = time.time()
TZ = ZoneInfo(cfg("site", "timezone", "UTC"))
TODAY = dt.datetime.now(TZ).strftime("%Y%m%d")
# <stamp>-<backend>-<role>.<kind>; names from before the backend switch (17 Oct) carry no backend
LOG_RE = re.compile(r"^(\d{8}-\d{6})-(?:([a-z]+)-)?([a-z]+)\.(json|txt|exit)(\.gz)?$")
ROLE_KEYS = ("impl", "arch", "qa", "adv", "verify", "overseer", "chat")
WEEK = 7 * 86400
COST_RE = re.compile(rb'"total_cost_usd"\s*:\s*([0-9.]+)')


def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def read_bytes(path):
    try:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rb") as fh:
            return fh.read()
    except OSError:
        return b""


def stamp_to_epoch(stamp):
    """Dispatcher log names carry the server's local time (site.timezone;
    checked against the .exit mtimes)."""
    return dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=TZ).timestamp()


def iso_to_epoch(text):
    if not text or text.startswith("0001-"):
        return None
    try:
        return dt.datetime.fromisoformat(text[:26].rstrip("Z") + "+00:00").timestamp()
    except ValueError:
        return None


def container(name):
    out = sh(["docker", "inspect", "--format", "{{json .State}}|{{.RestartCount}}", name])
    if out is None:
        return {"name": name, "state": "unknown"}
    state_json, _, restarts = out.strip().rpartition("|")
    st = json.loads(state_json)
    health = (st.get("Health") or {}).get("Status")
    return {"name": name, "state": st.get("Status", "unknown"), "health": health,
            "started": iso_to_epoch(st.get("StartedAt")),
            "restarts": int(restarts or 0), "oom": bool(st.get("OOMKilled"))}


def processes(name):
    out = sh(["docker", "top", name, "-eo", "args"])
    return out.splitlines()[1:] if out else []


def has_proc(procs, needle):
    return any(needle in p for p in procs)


# ---- agent-runner ---------------------------------------------------------

def sessions(repo_dir):
    """Dispatcher sessions from logs/, newest first: one entry per stem."""
    by_stem = {}
    for path in glob.glob(os.path.join(repo_dir, "dispatcher", "logs", "*")):
        m = LOG_RE.match(os.path.basename(path))
        if not m:
            continue
        # before 17 Oct every session ran on vibe (Mistral); the file name didn't say so
        stamp, backend, role, kind = m.group(1), m.group(2) or "vibe", m.group(3), m.group(4)
        s = by_stem.setdefault(stamp + role, {"stamp": stamp, "backend": backend,
                                              "role": role, "files": {}})
        s["files"][kind] = path
    out = []
    for s in sorted(by_stem.values(), key=lambda s: s["stamp"], reverse=True):
        start = stamp_to_epoch(s["stamp"])
        exit_path = s["files"].get("exit")
        entry = {"role": s["role"], "backend": s["backend"], "start": start,
                 "running": exit_path is None, "exit": None, "end": None, "cost": None}
        if exit_path:
            code = read_bytes(exit_path).strip()
            entry["exit"] = int(code) if code.lstrip(b"-").isdigit() else None
            entry["end"] = os.path.getmtime(exit_path)
        log_path = s["files"].get("json") or s["files"].get("txt")
        if log_path and (entry["running"] or NOW - start < WEEK or len(out) < 5):
            costs = COST_RE.findall(read_bytes(log_path))
            if costs:
                entry["cost"] = round(float(costs[-1]), 2)
        if entry["running"] and log_path:
            # A session killed without an .exit file looks running forever.
            entry["stale"] = NOW - os.path.getmtime(log_path) > 3 * 3600
        out.append(entry)
    return out


def project_repos():
    """The supervisor's PROJECT_REPOS (what it actually starts), read from its source."""
    try:
        text = open(os.path.join(RUNNER, "supervisor.py")).read()
        m = re.search(r"^PROJECT_REPOS\s*=\s*\[([^\]]*)\]", text, re.M)
        repos = re.findall(r"[\"']([\w.-]+)[\"']", m.group(1)) if m else []
        return repos or REPOS_FALLBACK
    except OSError:
        return REPOS_FALLBACK


def projects_on_disk():
    return sorted(d for d in os.listdir(PROJECTS)
                  if not d.startswith(".") and os.path.isdir(os.path.join(PROJECTS, d, "dispatcher")))


def activity(repo):
    best = None
    for label, rel in ACTIVITY.get(repo, []):
        path = os.path.join(PROJECTS, repo, rel)
        if os.path.exists(path):
            t = os.path.getmtime(path)
            if best is None or t > best["at"]:
                best = {"label": label, "at": t}
    return best


def worker(repo, procs, started=True):
    repo_dir = os.path.join(PROJECTS, repo)
    st = read_json(os.path.join(repo_dir, "dispatcher", "state.json")) or {}
    all_sessions = sessions(repo_dir)
    work = [s for s in all_sessions if s["role"] != "overseer"]
    running = [s for s in work if s["running"] and not s.get("stale")]
    # seen_blockers keeps closed ones forever; an open unanswered blocker is
    # re-reminded daily, so a fresh reminder stamp means it is still open.
    resumed = set(st.get("resumed_blockers", []))
    open_blockers = sorted(int(n) for n, t in (st.get("blocker_reminded") or {}).items()
                           if int(n) not in resumed and NOW - t < 26 * 3600)
    pause_until = st.get("quota_pause_until")
    failures = st.get("failures") or {}
    owner_flag = os.path.join(PROJECTS, ".control", f"pause-{repo}")
    owner_paused = os.path.exists(owner_flag)
    if not started:
        status = "off"           # on disk, but the supervisor doesn't start it
    elif owner_paused:
        status = "off"           # paused by the owner (supervisor pause flag)
    elif running:
        status = "running"
    elif open_blockers:
        status = "blocked"
    elif pause_until and pause_until > NOW:
        status = "paused"
    elif st.get("notified_done"):
        status = "done"
    else:
        status = "idle"
    today = [s for s in work if dt.datetime.fromtimestamp(s["start"], TZ).strftime("%Y%m%d") == TODAY]
    roles = {}
    for s in all_sessions:   # newest first
        r = roles.setdefault(s["role"], {"total": 0, "week": 0, "cost_week": 0.0, "fails_week": 0,
                                         "running": False, "last": None, "recent": []})
        r["total"] += 1
        if NOW - s["start"] < WEEK:
            r["week"] += 1
            r["cost_week"] += s["cost"] or 0
            r["fails_week"] += 1 if s["exit"] not in (0, None) else 0
        r["running"] = r["running"] or (s["running"] and not s.get("stale"))
        r["last"] = r["last"] or s
        if len(r["recent"]) < 3:
            r["recent"].append(s)
    for r in roles.values():
        r["cost_week"] = round(r["cost_week"], 2)
    return {
        "repo": repo, "status": status,
        "current": running[0] if running else None,
        "blockers": open_blockers,
        "pause_until": pause_until if pause_until and pause_until > NOW else None,
        "failures": {k: v for k, v in failures.items() if v},
        "approved_prs": st.get("approved_prs", []),
        "stash_pending": bool(st.get("stash_note_pending")),
        "sessions_today": len(today),
        "cost_today": round(sum(s["cost"] or 0 for s in today), 2),
        "recent": work[:6],
        "roles": roles,
        "started": started,
        "owner_paused": owner_paused,
        "paused_since": os.path.getmtime(owner_flag) if owner_paused else None,
        "activity": activity(repo),
        "app_decommissioned": repo in APP_DECOMMISSIONED,
        "process_seen": has_proc(procs, f"/{repo}/dispatcher") or has_proc(procs, f"projects/{repo}"),
    }


def env_keys(path, keys):
    """Values of a few named, non-secret keys from an .env file; every
    other line is skipped unread."""
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                k, sep, v = line.partition("=")
                if sep and k.strip() in keys:
                    out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def runner_settings():
    """Non-secret dispatcher settings: backend mode, per-role pins and limits."""
    pins = {f"AGENT_BACKEND_{r.upper()}": r for r in ROLE_KEYS}
    env = env_keys(os.path.join(RUNNER, ".env"), {"AGENT_BACKEND", "DISPATCH_MAX_PRICE", "DISPATCH_DAILY_SESSIONS",
                                                  "DISPATCH_MAX_TURNS_REVIEW", *pins})
    return {"backend": env.get("AGENT_BACKEND") or None,
            "role_pins": {role: env[k] for k, role in pins.items() if env.get(k)},
            "limits": {"max_price": env.get("DISPATCH_MAX_PRICE"), "daily_sessions": env.get("DISPATCH_DAILY_SESSIONS"),
                       "max_turns_review": env.get("DISPATCH_MAX_TURNS_REVIEW")}}


def cron_jobs():
    """The console user's crontab lines for CRON_JOBS: schedule and on/off."""
    out = {}
    for line in (sh(["crontab", "-l"]) or "").splitlines():
        for job_id, needle in CRON_JOBS.items():
            if needle not in line or job_id in out:
                continue
            body = line[len(CRON_OFF):] if line.startswith(CRON_OFF) else line.lstrip("# ")
            meta = (CFG.get("cron") or {}).get(job_id, {})
            out[job_id] = {"schedule": " ".join(body.split()[:5]),
                           "enabled": not line.lstrip().startswith("#"),
                           "match": needle, "project": meta.get("project"), "label": meta.get("label")}
    return out


def workflows():
    """Scheduled GitHub Actions in the project clones (main): calendar jobs
    that file `run` issues. Whether a gate variable is set lives on GitHub."""
    out = []
    for repo in project_repos():
        for path in sorted(glob.glob(os.path.join(PROJECTS, repo, ".github", "workflows", "*.y*ml"))):
            try:
                text = open(path).read()
            except OSError:
                continue
            crons = re.findall(r"cron:\s*[\"']([^\"']+)[\"']", text)
            if not crons:
                continue
            name = re.search(r"^name:\s*(.+)$", text, re.M)
            gate = re.search(r"vars\.(\w+)\s*==\s*'enabled'", text)
            out.append({"repo": repo, "file": os.path.basename(path),
                        "name": name.group(1).strip() if name else os.path.basename(path),
                        "crons": crons, "gate_var": gate.group(1) if gate else None})
    return out


def agent_runner():
    procs = processes(RUNNER_CT)
    ops = sessions(os.path.join(PROJECTS, OPS_REPO))
    overseer = [s for s in ops if s["role"] == "overseer"]
    budget = read_json(os.path.join(PROJECTS, ".session-budget.json")) or {}
    bridge_state = os.path.join(PROJECTS, ".telegram-bridge-state.json")
    return {
        "container": container(RUNNER_CT),
        **runner_settings(),
        "budget_today": budget.get(dt.datetime.now(TZ).date().isoformat(), {}).get("sessions"),
        "workers": [worker(r, procs) for r in project_repos()]
                   + [worker(r, procs, started=False) for r in projects_on_disk() if r not in project_repos()],
        # .control/ is created by a supervisor that knows the pause flags
        "pause_hook": os.path.isdir(os.path.join(PROJECTS, ".control")),
        "overseer": {"last": overseer[0] if overseer else None,
                     "running": has_proc(procs, "run-overseer") or bool(overseer and overseer[0]["running"]),
                     "recent": overseer[:5]},
        "bridge": {"running": has_proc(procs, "telegram_bridge"),
                   "last_activity": os.path.getmtime(bridge_state) if os.path.exists(bridge_state) else None},
        "memory": {"running": has_proc(procs, "agent_memory") or has_proc(procs, "memory")},
        "workflows": workflows(),
        "cron": cron_jobs(),
    }


# ---- job-bot --------------------------------------------------------------

def job_bot():
    c = container(JOBBOT_CT)
    logs = sh(["docker", "logs", "--since", "96h", JOBBOT_CT], timeout=20) or ""
    lines = logs.splitlines()
    last_start = next((ln for ln in reversed(lines) if ln.startswith("[run] start")), None)
    last_new = next((ln for ln in reversed(lines) if re.match(r"\[run\] \d+ new of", ln)), None)
    next_run = next((ln for ln in reversed(lines) if ln.startswith("[daemon] next run at")), None)
    failed = next((ln for ln in reversed(lines) if ln.startswith("[daemon] run failed")), None)
    mail_fail = sum(1 for ln in lines[-400:] if "email status check failed" in ln)
    m_start = re.search(r"start (\S+)", last_start or "")
    m_new = re.match(r"\[run\] (\d+) new of (\d+)", last_new or "")
    m_next = re.search(r"next run at (\S+ \S+)", next_run or "")
    env = env_keys(JOBBOT_ENV, {"RUN_AT", "EMAIL_CHECK_INTERVAL_S", "TZ"})
    return {
        "container": c,
        "run_at": env.get("RUN_AT", "07:00"),
        "mail_interval_s": int(env.get("EMAIL_CHECK_INTERVAL_S", "3600") or 0),
        "tz": env.get("TZ", cfg("site", "timezone", "UTC")),
        "last_run": m_start.group(1) if m_start else None,
        "last_run_new": int(m_new.group(1)) if m_new else None,
        "last_run_seen": int(m_new.group(2)) if m_new else None,
        "next_run": m_next.group(1) if m_next else None,
        "last_failure": failed[len("[daemon] run failed: "):][:160] if failed else None,
        "mail_check_failures_recent": mail_fail,
    }


# ---- claude-agent ---------------------------------------------------------

def first_message(raw):
    """Opening words of a chat, used as its name when it has no title."""
    try:
        content = json.loads(raw).get("message", {}).get("content")
    except (ValueError, AttributeError):
        return None
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content
                           if isinstance(c, dict) and c.get("type") == "text")
    if not isinstance(content, str):
        return None
    text = re.sub(r"<([a-z_-]+)[^>]*>.*?</\1>", " ", content, flags=re.S)
    text = " ".join(text.split())
    return (text[:70] + "…" if len(text) > 70 else text) or None


def chats():
    out = []
    for path in sorted(glob.glob(os.path.join(CHATS, "*.jsonl")),
                       key=os.path.getmtime, reverse=True)[:12]:
        mtime = os.path.getmtime(path)
        title, entry, first, started = None, None, None, None
        try:
            with open(path, "rb") as fh:
                for raw in fh:
                    if started is None and b'"timestamp":"' in raw:
                        m = re.search(rb'"timestamp":"([^"]+)"', raw)
                        started = iso_to_epoch(m.group(1).decode()) if m else None
                    if first is None and b'"type":"user"' in raw:
                        first = first_message(raw)
                    if entry is None and b'"entrypoint"' in raw:
                        m = re.search(rb'"entrypoint":"([^"]+)"', raw)
                        entry = m.group(1).decode() if m else None
                    if b'"customTitle"' in raw:
                        m = re.search(rb'"customTitle":"((?:[^"\\]|\\.)*)"', raw)
                        if m:
                            title = json.loads(b'"' + m.group(1) + b'"')
        except OSError:
            continue
        out.append({"id": os.path.basename(path)[:-len(".jsonl")], "title": title or first,
                    "kind": {"sdk-cli": "Remote Control", "cli": "terminal"}.get(entry, entry),
                    "started": started, "last_activity": mtime, "active": NOW - mtime < 15 * 60})
    return out


def claude_agent():
    procs = processes(CLAUDE_CT)
    return {"container": container(CLAUDE_CT),
            "rc_loop": has_proc(procs, "rc-loop"),
            "rc_running": has_proc(procs, "remote-control"),
            "claude_processes": sum(1 for p in procs if re.search(r"(^|/)claude(\s|$)", p)),
            "chats": chats()}


# ---- Overwatch ------------------------------------------------------------

def systemd_props(unit, props):
    out = sh(["systemctl", "show", unit, "-p", ",".join(props)])
    vals = {}
    for line in (out or "").splitlines():
        k, _, v = line.partition("=")
        vals[k] = v
    return vals


def us_to_epoch(v):
    try:
        n = int(v)
        return n / 1e6 if n else None
    except (TypeError, ValueError):
        return None


def overwatch():
    tiers = []
    for t in OVERWATCH_TIERS:
        timer = systemd_props(f"{OW_UNIT}{t}.timer",
                              ["ActiveState", "NextElapseUSecRealtime", "LastTriggerUSec",
                               "TimersCalendar", "UnitFileState"])
        cal = re.search(r"OnCalendar=(.+?)\s*;", timer.get("TimersCalendar", ""))
        svc = systemd_props(f"{OW_UNIT}{t}.service",
                            ["Result", "ExecMainStatus", "ActiveState", "ExecMainExitTimestamp"])
        tiers.append({"tier": t, "timer": timer.get("ActiveState") or "unknown",
                      "calendar": cal.group(1) if cal else None,
                      "enabled": timer.get("UnitFileState") == "enabled" if timer.get("UnitFileState") else None,
                      "next": None, "last": us_to_epoch(timer.get("LastTriggerUSec")),
                      "running": svc.get("ActiveState") == "activating",
                      "result": svc.get("Result") or None,
                      "exit": int(svc["ExecMainStatus"]) if svc.get("ExecMainStatus", "").isdigit() else None})
        nxt = sh(["systemctl", "list-timers", f"{OW_UNIT}{t}.timer", "--all",
                  "--output=json"])
        try:
            rows = json.loads(nxt) if nxt else []
            if rows and rows[0].get("next"):
                tiers[-1]["next"] = rows[0]["next"] / 1e6
        except (ValueError, TypeError, KeyError):
            pass
    return {"tiers": tiers}


# ---- Infisical: key names per template (never values) -------------------------

def infisical():
    """Which keys each Infisical template feeds into which file. Secrets are
    references in the template, so only their names are read; for a few known
    settings the plain value is shown. For each secret the rendered file is
    checked for a non-empty line (presence only; the value stays in memory)."""
    dst = {}
    try:
        agent_cfg = open(INFISICAL_AGENT_YAML).read()
        # infisical_setup.py writes: - source-path: "<tmpl>" / destination-path: "<file>"
        for src, target in re.findall(r'source-path:\s*"?([^"\s]+\.tmpl)"?\s*\n\s*destination-path:\s*"?([^"\s]+)"?', agent_cfg):
            dst[os.path.basename(src)[:-5]] = target
    except OSError:
        pass
    out = []
    for path in sorted(glob.glob(os.path.join(INFISICAL_TEMPLATES, "*.tmpl"))):
        name = os.path.basename(path)[:-5]
        secrets, settings, folders = [], [], set()
        try:
            lines = open(path).read().splitlines()
        except OSError:
            continue
        for line in lines:
            m = SECRET_REF.match(line)
            if m:
                secrets.append(m.group(1)); folders.add("/" + m.group(2))
                continue
            m = PLAIN.match(line)
            if m and not line.lstrip().startswith("#"):
                k = m.group(1)
                settings.append({"key": k, "value": m.group(2).strip().strip("'\"") if k in SHOW_VALUES else None})
        rendered = {}
        target = dst.get(name)
        if target and os.path.exists(target):
            have = {}
            try:
                for line in open(target):
                    k, sep, v = line.partition("=")
                    if sep:
                        have[k.strip().removeprefix("export ").strip()] = bool(v.strip().strip("'\""))
            except OSError:
                have = None
            if have is not None:
                rendered = {k: ("ok" if have.get(k) else "empty" if k in have else "missing") for k in secrets}
        out.append({"name": name, "folders": sorted(folders), "secrets": secrets, "settings": settings,
                    "dst": target, "rendered": rendered, "changed": os.path.getmtime(path),
                    "rendered_at": os.path.getmtime(target) if target and os.path.exists(target) else None})
    agent = systemd_props(cfg("infisical", "unit", "infisical-agent") + ".service", ["ActiveState", "ActiveEnterTimestamp"])
    return {"templates": out, "agent": agent.get("ActiveState") or "unknown", "readable": bool(out)}


# ---- ledger: first/last seen per item, kept after removal ---------------------

def inventory(status):
    """Everything the console shows that can appear or disappear, keyed by a
    stable id. `known` lists the kinds whose source was readable this run, so
    an unreadable source (docker or systemd down) never looks like removals."""
    items, known = {}, {"project", "role", "workflow", "chat"}
    ar = status["agent_runner"]

    def add(i, kind, name, where, detail=""):
        items[i] = {"kind": kind, "name": name, "where": where, "detail": detail, "evidence": None}

    for c in (ar["container"], status["job_bot"]["container"], status["claude_agent"]["container"]):
        if c["state"] != "unknown":
            known.add("container")
            add("container:" + c["name"], "container", c["name"], "Docker", c["state"])
    for w in ar["workers"]:
        add("project:" + w["repo"], "project", w["repo"], RUNNER_CT, w["status"])
        for role, r in w["roles"].items():
            add(f"role:{w['repo']}:{role}", "role", role, w["repo"], f"{r['total']} sessions")
    for wf in ar.get("workflows", []):
        add(f"workflow:{wf['repo']}:{wf['file']}", "workflow", wf["name"], wf["repo"], ", ".join(wf["crons"]))
    if sh(["crontab", "-l"]) is not None:
        known.add("cron")
        for job, c in (ar.get("cron") or {}).items():
            add("cron:" + job, "cron", job, "crontab", c["schedule"] + ("" if c["enabled"] else " (off)"))
    for t in status["overwatch"]["tiers"]:
        if t["timer"] != "unknown":
            known.add("timer")
            add("timer:overwatch-" + t["tier"], "timer", OW_UNIT + t["tier"], "systemd", t.get("calendar") or "")
    inf = status.get("infisical") or {}
    if inf.get("readable"):
        known.update({"template", "secret"})
        for t in inf["templates"]:
            add("template:" + t["name"], "template", t["name"], "Infisical", f"{len(t['secrets'])} secrets → {t['dst'] or '?'}")
            for k in t["secrets"]:
                add(f"secret:{t['name']}:{k}", "secret", k, "Infisical " + (", ".join(t["folders"]) or t["name"]))
    for path in glob.glob(os.path.join(CHATS, "*.jsonl")):
        cid = os.path.basename(path)[:-len(".jsonl")]
        add("chat:" + cid, "chat", cid[:8], CLAUDE_CT)
    for c in status["claude_agent"]["chats"]:
        if "chat:" + c["id"] in items:
            items["chat:" + c["id"]].update(name=c["title"] or c["id"][:8], detail=c["kind"] or "chat", evidence=c["started"])
    return items, known


def update_ledger(items, known):
    """Merge this run into the ledger. A missing ledger is seeded from what
    exists now, dated only where the data itself shows when it began (chat
    start, first session), so day one isn't all 'new'."""
    ledger = read_json(LEDGER)
    seeding = not isinstance(ledger, dict)
    ledger = {} if seeding else ledger
    for i, it in items.items():
        e = ledger.get(i)
        if e is None:
            e = ledger[i] = {"first_seen": it["evidence"] if seeding else (it["evidence"] or NOW)}
        e.update(kind=it["kind"], name=it["name"], where=it["where"], detail=it["detail"], last_seen=NOW)
        e.pop("missing_since", None)
    for i, e in ledger.items():
        if i not in items and e.get("kind") in known and "missing_since" not in e:
            e["missing_since"] = NOW
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(ledger, fh, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, LEDGER)
    return {i: e for i, e in ledger.items()}


def site():
    """What the page needs to know about this site: names, hosts, links and the
    catalog of descriptions (a private JSON file; the page has generic fallbacks)."""
    try:
        with open(CATALOG) as fh:
            catalog = json.load(fh)
    except (OSError, ValueError):
        catalog = {}
    return {"name": cfg("site", "name", "agents"), "timezone": cfg("site", "timezone", "UTC"),
            "host": cfg("site", "host", ""), "github_owner": cfg("site", "github_owner", ""),
            "ops_repo": OPS_REPO, "containers": {"runner": RUNNER_CT, "jobbot": JOBBOT_CT, "claude": CLAUDE_CT},
            "ports": cfg("site", "ports", {}), "overwatch_unit": OW_UNIT,
            "infisical_unit": cfg("infisical", "unit", "infisical-agent"),
            "paths": {"projects": PROJECTS, "infisical_templates": INFISICAL_TEMPLATES,
                      "install": cfg("paths", "install", "/opt/agent-console"),
                      "password": cfg("console", "password_file", "/etc/agent-console/password"),
                      "log": cfg("console", "log", "/var/lib/agent-console/actions.log")},
            "catalog": catalog}


def main():
    status = {"generated": NOW, "site": site(), "agent_runner": agent_runner(), "job_bot": job_bot(),
              "claude_agent": claude_agent(), "overwatch": overwatch(), "infisical": infisical()}
    os.makedirs(OUT, exist_ok=True)
    ledger = update_ledger(*inventory(status))
    status["ledger"] = ledger
    status["removed_after_s"] = MISSING_AFTER_S
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, ".status.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(status, fh, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(OUT, "status.json"))


if __name__ == "__main__":
    main()
