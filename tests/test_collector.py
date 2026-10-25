"""agent-status.py on the fake server: what it reports, and that values stay out of status.json.
Also runs the page against that status.json (node, if installed)."""

import json
import shutil
import subprocess
import sys

import pytest

from conftest import CONSOLE, ROOT, SECRETS


@pytest.fixture
def status(site):
    r = subprocess.run([sys.executable, str(CONSOLE / "agent-status.py")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads((site["web"] / "status.json").read_text())


def test_workers_and_their_states(status):
    ar = status["agent_runner"]
    by = {w["repo"]: w for w in ar["workers"]}
    assert by["alpha"]["status"] == "done" and by["alpha"]["started"]
    assert by["beta"]["status"] == "running" and by["beta"]["blockers"] == [41]
    assert by["gamma"]["status"] == "off" and by["gamma"]["started"] is False      # on disk, not started
    assert by["alpha"]["activity"]["label"] == "nightly export"
    impl = by["alpha"]["roles"]["impl"]
    assert impl["week"] == 1 and impl["cost_week"] == 1.25
    assert by["alpha"]["roles"]["qa"]["recent"][0]["backend"] == "vibe"            # old log name without a backend
    assert ar["backend"] == "both" and ar["role_pins"] == {"adv": "claude"}
    assert ar["bridge"]["running"] and ar["memory"]["running"] and ar["pause_hook"] is False


def test_schedules(status):
    ar = status["agent_runner"]
    assert ar["workflows"][0]["repo"] == "alpha" and ar["workflows"][0]["crons"] == ["10 0 * * *"]
    assert ar["workflows"][0]["gate_var"] == "ALPHA_NIGHTLY"
    assert ar["cron"]["nightly-backup"] == {"schedule": "17 4 1 * *", "enabled": True, "match": "nightly-backup.sh",
                                            "project": "alpha", "label": "nightly backup"}
    assert ar["cron"]["disabled"]["enabled"] is False
    assert status["job_bot"]["run_at"] == "07:30" and status["job_bot"]["next_run"] == "2026-10-24 07:30:00"
    daily = status["overwatch"]["tiers"][0]
    assert daily["calendar"] == "*-*-* 07:00:00" and daily["enabled"] and daily["next"]


def test_chats(status):
    chat = status["claude_agent"]["chats"][0]
    assert chat["title"] == "Test chat" and chat["kind"] == "Remote Control"
    assert status["claude_agent"]["rc_running"] and status["claude_agent"]["rc_loop"]
    assert status["claude_agent"]["claude_processes"] == 2           # pids stripped before matching


def test_infisical_reports_names_never_values(status):
    inf = {t["name"]: t for t in status["infisical"]["templates"]}
    runner = inf["agent-runner"]
    assert runner["secrets"] == ["MISTRAL_API_KEY"] and runner["folders"] == ["/agent-runner"]
    assert runner["rendered"] == {"MISTRAL_API_KEY": "ok"}
    settings = {s["key"]: s["value"] for s in runner["settings"]}
    assert settings == {"AGENT_BACKEND": "both", "NTFY_TOPIC": None}                  # only known settings show values


def test_no_secret_value_reaches_the_page(status, site):
    text = (site["web"] / "status.json").read_text() + (site["web"] / "ledger.json").read_text()
    for value in SECRETS:
        assert value not in text


def test_host_facts(status):
    h = status["host"]
    assert h["hostname"] and h["kernel"] and h["cpus"] >= 1
    assert h["disks"][0]["path"] == "/" and h["disks"][0]["total"] > 0
    assert h["containers"] == {"running": 3, "total": 4}
    assert h["memory"]["total"] > 0 and h["uptime_s"] >= 0


def test_site_block_and_ledger(status):
    site = status["site"]
    assert site["github_owner"] == "example" and site["ops_repo"] == "alpha"
    assert site["catalog"]["items"]["overseer"]["head"].startswith("Catalog head")
    assert site["paths"]["projects"].endswith("/runner/projects")
    led = status["ledger"]
    assert "project:alpha" in led and "cron:nightly-backup" in led and "template:agent-runner" in led


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_renders_every_view(status, site):
    r = subprocess.run(["node", str(ROOT / "tests" / "page_smoke.js"), str(ROOT / "console" / "index.html"),
                        str(site["web"] / "status.json")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    agents = {i["id"]: i for i in out["agents:location"]}
    assert {"agent-runner", "w-alpha", "w-beta", "w-gamma", "overseer", "job-bot", "claude-agent", "ow-daily"} <= set(agents)
    assert agents["overseer"]["head"] == "Catalog head for the overseer."           # catalog wording wins
    assert agents["w-gamma"]["section"] == "Decommissioned"
    assert {i["id"] for i in out["schedules:location"]} >= {"sch-cron-nightly-backup", "sch-wf-alpha-nightly", "sch-ow-daily"}
    conns = {i["id"] for i in out["connections:location"]}
    assert "cn-gh" in conns and "cn-inf-agent-runner" in conns and "cn-console" in conns
    assert out["search"] > 20
    assert "Disk /" in out["account"] and "3 running of 4" in out["account"] and "agent-console 0.1.0" in out["account"]
    assert out["ask"].startswith("Why is it down?\n\nWhat the agent console shows") and "- Item: " in out["ask"]
    ui = out["ui"]
    assert ui["detailHidden"] and ui["cards"] == len(out["agents:location"])        # nothing open at start; every item a card
    assert ui["groups"][0] == "" and "agent-runner" in ui["groups"]                   # "All" first, then the groups
    assert ui["chips"][0] == "all" and "running" in ui["chips"]
    assert ui["rows"] == ui["cards"]                                                   # the list shows the same items
    assert 0 < ui["groupCards"] < ui["cards"] and ui["title"] == ui["groups"][1]
    assert ui["runningCards"] == sum(1 for i in out["agents:location"] if i["status"] == "running")
    assert ui["detailOpen"] and ui["detailClosed"]


def test_example_config_parses():
    import tomllib
    cfg = tomllib.loads((ROOT / "examples" / "console.example.toml").read_text())
    assert cfg["site"]["timezone"] == "UTC" and cfg["paths"]["web"].startswith("/var/lib/agent-console")


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_renders_with_the_example_catalog(status, site, tmp_path):
    status["site"]["catalog"] = json.loads((ROOT / "examples" / "catalog.example.json").read_text())
    path = tmp_path / "status-example.json"
    path.write_text(json.dumps(status))
    r = subprocess.run(["node", str(ROOT / "tests" / "page_smoke.js"), str(ROOT / "console" / "index.html"), str(path)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    conns = {i["id"]: i for i in out["connections:location"]}
    assert conns["cn-tg-bridge"]["section"] == "Telegram bots" and "cn-gh-agent" in conns
    sched = {i["id"]: i for i in out["schedules:location"]}
    assert sched["sch-cron-nightly-backup"]["name"] == "Nightly backup"
    roles = {i["id"]: i for i in out["agents:roles"]}
    assert roles["role-overseer"]["head"].startswith("Assesses")


def test_missing_sources_show_as_unknown_not_a_crash(site, tmp_path, monkeypatch):
    """A fresh server: no runner checkout, no chats, no templates, no crontab, docker down."""
    cfg = tmp_path / "empty.toml"
    web = tmp_path / "web-empty"
    cfg.write_text(f'[paths]\nweb = "{web}"\n[runner]\ndir = "{tmp_path}/no-runner"\n[claude]\nchats = "{tmp_path}/no-chats"\n'
                   f'[infisical]\ntemplates = "{tmp_path}/no-templates"\n[jobbot]\nenv = "{tmp_path}/no.env"\n')
    monkeypatch.setenv("AGENT_CONSOLE_CONFIG", str(cfg))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")        # no docker, crontab or systemctl fakes
    r = subprocess.run([sys.executable, str(CONSOLE / "agent-status.py")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    status = json.loads((web / "status.json").read_text())
    assert status["agent_runner"]["container"]["state"] == "unknown"
    assert status["agent_runner"]["workers"] == [] and status["infisical"]["readable"] is False
