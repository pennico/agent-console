"""Shared fixtures: a fake server in a temp folder.

`site` builds what the console reads on a real host (a runner checkout with
projects and dispatcher logs, job-bot's .env, chat transcripts, Infisical
templates) plus fake `docker`, `crontab`, `sudo`, `systemctl` and
`systemd-analyze` commands that log their arguments to calls.log. Values that
must never reach the page are planted as SECRET-… strings.
"""

import importlib.util
import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "console"
SECRETS = ("SECRET-mistral-key", "SECRET-rendered-value", "SECRET-topic-name", "SECRET-jobbot-token")

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open(os.environ["FAKE_CALLS"], "a").write("docker " + " ".join(a) + "\n")
if a[:1] == ["inspect"]:
    print(json.dumps({"Status": "running", "StartedAt": "2026-10-20T10:00:00.000000000Z", "OOMKilled": False}) + "|0")
elif a[:1] == ["top"]:
    # like the real daemon: ps options must yield a PID column
    if len(a) > 2 and "pid" not in a[-1].lower().split(","):
        sys.stderr.write("Error response from daemon: Couldn't find PID field in ps output\n"); sys.exit(1)
    print("PID COMMAND")
    if a[1] == "agent-runner":
        print("101 python3 -u /app/supervisor.py\n102 python3 -u /app/telegram_bridge.py\n"
              "103 python3 -u /app/agent_memory/server.py\n104 bash /app/projects/beta/dispatcher/run.sh")
    elif a[1] == "claude-agent":
        print("201 bash rc-loop.sh\n202 claude remote-control --name test\n203 claude")
elif a[:1] == ["logs"]:
    print("[run] start 2026-10-23T07:30:00\n[run] 4 new of 120\n[daemon] next run at 2026-10-24 07:30:00")
elif a[:2] == ["exec", "agent-runner"] and "grep" in a:
    sys.exit(0)
'''

FAKE_CRONTAB = r'''#!/usr/bin/env python3
import os, sys
tab = os.environ["FAKE_CRONTAB"]
open(os.environ["FAKE_CALLS"], "a").write("crontab " + " ".join(sys.argv[1:]) + "\n")
if sys.argv[1:] == ["-l"]:
    if not os.path.exists(tab):
        sys.exit(1)
    sys.stdout.write(open(tab).read())
elif sys.argv[1:] == ["-"]:
    open(tab, "w").write(sys.stdin.read())
'''

FAKE_LOGGER = r'''#!/usr/bin/env python3
import os, sys
open(os.environ["FAKE_CALLS"], "a").write(os.path.basename(sys.argv[0]) + " " + " ".join(sys.argv[1:]) + "\n")
'''

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open(os.environ["FAKE_CALLS"], "a").write("systemctl " + " ".join(a) + "\n")
if a[:1] == ["show"]:
    unit = a[1]
    if unit.endswith(".timer"):
        print("ActiveState=active\nUnitFileState=enabled\nLastTriggerUSec=1761300000000000\n"
              "TimersCalendar={ OnCalendar=*-*-* 07:00:00 ; next_elapse=Sat 2026-10-25 07:00:00 CEST }")
    elif unit.endswith(".service"):
        print("Result=success\nExecMainStatus=0\nActiveState=inactive")
elif a[:1] == ["list-timers"]:
    print(json.dumps([{"next": 1761400000000000}]))
'''


def _exe(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A fake server; returns a dict of its paths. Sets PATH, AGENT_CONSOLE_CONFIG and friends."""
    t = tmp_path
    now = time.time()
    bin_ = t / "bin"; bin_.mkdir()
    _exe(bin_ / "docker", FAKE_DOCKER)
    _exe(bin_ / "crontab", FAKE_CRONTAB)
    _exe(bin_ / "systemctl", FAKE_SYSTEMCTL)
    for name in ("sudo", "systemd-analyze", "agents-mode", "infisical-restart"):
        _exe(bin_ / name, FAKE_LOGGER)

    runner = t / "runner"
    projects = runner / "projects"
    (runner / "supervisor.py").parent.mkdir(parents=True)
    (runner / "supervisor.py").write_text('PROJECT_REPOS = ["alpha", "beta"]\nCONTROL_DIR = "/app/projects/.control"\n')
    (runner / ".env").write_text("AGENT_BACKEND=both\nAGENT_BACKEND_ADV=claude\nMISTRAL_API_KEY=SECRET-mistral-key\nDISPATCH_MAX_PRICE=5\n")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now - 3600))
    old = time.strftime("%Y%m%d-%H%M%S", time.localtime(now - 20 * 86400))
    for repo in ("alpha", "beta", "gamma"):
        (projects / repo / "dispatcher" / "logs").mkdir(parents=True)
    logs = projects / "alpha" / "dispatcher" / "logs"
    (logs / f"{stamp}-claude-impl.json").write_text('{"type":"result","total_cost_usd": 1.25}')
    (logs / f"{stamp}-claude-impl.exit").write_text("0")
    (logs / f"{old}-qa.txt").write_text("old session from before the backend name")
    (logs / f"{old}-qa.exit").write_text("1")
    (projects / "alpha" / "dispatcher" / "state.json").write_text(json.dumps({"notified_done": True}))
    (projects / "alpha" / ".github" / "workflows").mkdir(parents=True)
    (projects / "alpha" / ".github" / "workflows" / "nightly.yml").write_text(
        "name: nightly\non:\n  schedule:\n    - cron: '10 0 * * *'\n"
        "jobs:\n  run:\n    if: vars.ALPHA_NIGHTLY == 'enabled'\n    runs-on: ubuntu-latest\n")
    beta = projects / "beta" / "dispatcher"
    (beta / "logs" / f"{stamp}-vibe-impl.json").write_text("{}")
    (beta / "state.json").write_text(json.dumps({"blocker_reminded": {"41": now - 3600}}))
    (projects / "alpha" / "out").mkdir()
    (projects / "alpha" / "out" / "export.log").write_text("ok")

    jobbot_env = t / "job-bot.env"
    jobbot_env.write_text("RUN_AT=07:30\nEMAIL_CHECK_INTERVAL_S=3600\nTELEGRAM_BOT_TOKEN=SECRET-jobbot-token\n")

    chats = t / "chats"; chats.mkdir()
    compact = {"separators": (",", ":")}   # transcripts are compact JSON lines
    (chats / "11111111-2222-3333-4444-555555555555.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2026-10-23T08:00:00Z", "entrypoint": "sdk-cli",
                    "message": {"content": "Hello from a test chat"}}, **compact) + "\n"
        + json.dumps({"type": "summary", "customTitle": "Test chat"}, **compact) + "\n")

    tmpl = t / "templates"; tmpl.mkdir()
    rendered = t / "rendered-runner.env"
    (tmpl / "agent-runner.tmpl").write_text(
        'MISTRAL_API_KEY={{ with getSecretByName "pid" "prod" "/agent-runner" "MISTRAL_API_KEY" }}{{ .Value }}{{ end }}\n'
        "AGENT_BACKEND=both\nNTFY_TOPIC=SECRET-topic-name\n")
    (tmpl / "job-bot.tmpl").write_text("RUN_AT=07:30\nEMAIL_CHECK_INTERVAL_S=3600\n")
    for p in tmpl.iterdir():
        p.chmod(0o600)
    rendered.write_text("MISTRAL_API_KEY=SECRET-rendered-value\nAGENT_BACKEND=both\n")
    agent_yaml = t / "agent.yaml"
    agent_yaml.write_text(f'templates:\n  - source-path: "{tmpl}/agent-runner.tmpl"\n    destination-path: "{rendered}"\n')

    crontab = t / "crontab.txt"
    crontab.write_text("# m h dom mon dow command\n17 4 1 * * /home/u/bin/nightly-backup.sh >> /tmp/b.log 2>&1\n"
                       "#0 3 * * * /home/u/bin/disabled-by-hand.sh\n")
    catalog = t / "catalog.json"
    catalog.write_text(json.dumps({"items": {"overseer": {"head": "Catalog head for the overseer."}},
                                   "connections": [{"key": "gh", "section": "GitHub", "name": "Agent token", "tags": ["GitHub"],
                                                    "users": ["agent-runner"], "cred": "GH_TOKEN", "stored": "runner .env"}]}))
    web = t / "web"; web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>test</title>")
    run_dir = t / "run"; run_dir.mkdir()
    state = t / "state"; state.mkdir()
    password = t / "password"
    log = t / "actions.log"
    calls = t / "calls.log"; calls.write_text("")

    config = t / "console.toml"
    config.write_text(textwrap.dedent(f"""\
        [site]
        name = "test"
        timezone = "UTC"
        github_owner = "example"
        catalog = "{catalog}"
        [paths]
        web = "{web}"
        install = "{t}"
        [console]
        runner_sock = "{run_dir}/runner.sock"
        passkeys = "{state}/passkeys.json"
        collector = "{t}/no-collector.py"
        log = "{log}"
        password_file = "{password}"
        [runner]
        dir = "{runner}"
        ops_repo = "alpha"
        mode_command = "{bin_}/agents-mode"
        [runner.activity]
        alpha = [["nightly export", "out/export.log"]]
        [jobbot]
        env = "{jobbot_env}"
        [claude]
        chats = "{chats}"
        [infisical]
        templates = "{tmpl}"
        agent_yaml = "{agent_yaml}"
        restart = ["{bin_}/infisical-restart"]
        [overwatch]
        tiers = ["daily", "weekly"]
        [cron.nightly-backup]
        match = "nightly-backup.sh"
        project = "alpha"
        label = "nightly backup"
        [cron.disabled]
        match = "disabled-by-hand.sh"
        """))
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENT_CONSOLE_CONFIG", str(config))
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    monkeypatch.setenv("FAKE_CRONTAB", str(crontab))
    for k in ("AGENT_CONSOLE_OUT", "AGENT_RUNNER_DIR", "AGENT_PROJECTS_DIR", "JOBBOT_ENV", "CLAUDE_CHATS_DIR",
              "INFISICAL_TEMPLATES", "AGENT_CONSOLE_CATALOG", "CONSOLE_WEB", "CONSOLE_LOG", "CONSOLE_PASSWORD_FILE"):
        monkeypatch.delenv(k, raising=False)
    return {"root": t, "bin": bin_, "runner": runner, "projects": projects, "templates": tmpl, "web": web, "calls": calls,
            "crontab": crontab, "config": config, "log": log, "password": password, "run": run_dir, "state": state,
            "catalog": catalog}


def load(name, module_name=None):
    """Import console/<name>.py fresh (it reads the config at import time)."""
    module_name = module_name or name.replace("-", "_") + "_" + str(time.monotonic_ns())
    spec = importlib.util.spec_from_file_location(module_name, CONSOLE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def calls(site):
    return site["calls"].read_text().splitlines()
