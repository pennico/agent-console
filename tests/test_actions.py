"""The runner's allow-list: perform() is the trust boundary for every button."""

import json
import os
import stat

import pytest

from conftest import calls, load


@pytest.fixture
def console(site, monkeypatch):
    mod = load("agent-console")
    monkeypatch.setattr(mod, "refresh_status", lambda: None)
    return mod


@pytest.mark.parametrize("action,target,value", [
    ("nope", "agent-runner", ""),                 # unknown action
    ("restart", "some-other-container", ""),      # target outside the list
    ("restart", "agent-runner", "x"),             # a value where none is allowed
    ("jobbot-time", "job-bot", "25:00"),
    ("jobbot-time", "job-bot", "07:00; rm -rf /"),
    ("jobbot-mail", "job-bot", "60"),
    ("role-model", "impl", "gpt"),
    ("role-model", "nope", "claude"),
    ("cron", "nightly-backup", "* * * * * evil"),
    ("cron", "nightly-backup", "$(reboot) * * * *"),
    ("cron", "unknown-job", "on"),
    ("pause", "gamma", ""),                       # on disk but not in PROJECT_REPOS
    ("pause", "../etc", ""),
    ("overwatch-calendar", "daily", "07:00\nExecStart=/bin/sh"),
    ("overwatch-run", "hourly", ""),
    ("backend", "rm", ""),
])
def test_refuses_anything_off_the_list(console, site, action, target, value):
    code, body = console.perform(action, target, value, "owner@example.com")
    assert code == 400, body
    assert calls(site) == []
    assert not site["log"].exists()


def test_restart_runs_docker_and_is_audited(console, site):
    code, body = console.perform("restart", "agent-runner", "", "owner@example.com")
    assert code == 200, body
    assert "docker restart --time 60 agent-runner" in calls(site)
    entry = json.loads(site["log"].read_text().splitlines()[-1])
    assert entry["action"] == "restart" and entry["target"] == "agent-runner" and entry["exit"] == 0
    assert entry["login"] == "owner@example.com"


def test_pause_resume_and_stop_session_use_flag_files(console, site):
    assert console.perform("pause", "beta", "", "o")[0] == 200
    assert console.perform("stop-session", "alpha", "", "o")[0] == 200
    assert console.perform("resume", "beta", "", "o")[0] == 200
    log = calls(site)
    assert "docker exec agent-runner touch /app/projects/.control/pause-beta" in log
    assert "docker exec agent-runner touch /app/projects/.control/stop-alpha" in log
    assert "docker exec agent-runner rm -f /app/projects/.control/pause-beta" in log


def test_overseer_runs_in_the_ops_repo(console, site):
    assert console.perform("overseer-now", "alpha", "", "o")[0] == 200
    assert "docker exec -d -w /app/projects/alpha agent-runner bash dispatcher/run-overseer.sh" in calls(site)


def test_role_model_pins_and_unpins_in_the_template(console, site):
    tmpl = site["templates"] / "agent-runner.tmpl"
    before = tmpl.read_text()
    assert console.perform("role-model", "impl", "claude", "o")[0] == 200
    text = tmpl.read_text()
    assert "AGENT_BACKEND_IMPL=claude\n" in text
    assert text.startswith(before)                       # every other line untouched
    assert stat.S_IMODE(os.stat(tmpl).st_mode) == 0o600  # mode kept
    assert any(c.startswith("infisical-restart") for c in calls(site))
    assert console.perform("role-model", "impl", "vibe", "o")[0] == 200
    assert tmpl.read_text().count("AGENT_BACKEND_IMPL=") == 1
    assert console.perform("role-model", "impl", "default", "o")[0] == 200
    assert tmpl.read_text() == before


def test_jobbot_settings_edit_one_line(console, site):
    tmpl = site["templates"] / "job-bot.tmpl"
    assert console.perform("jobbot-time", "job-bot", "06:15", "o")[0] == 200
    assert console.perform("jobbot-mail", "job-bot", "7200", "o")[0] == 200
    assert tmpl.read_text() == "RUN_AT=06:15\nEMAIL_CHECK_INTERVAL_S=7200\n"


def test_run_window_blocks_changes_next_to_the_scheduled_run(console, monkeypatch):
    monkeypatch.setattr(console, "jobbot_window", lambda: True)
    code, body = console.perform("jobbot-time", "job-bot", "06:15", "o")
    assert code == 409


def test_cron_reschedules_pauses_and_resumes_one_line(console, site):
    tab = site["crontab"]
    assert console.perform("cron", "nightly-backup", "30 5 2 * *", "o")[0] == 200
    assert "30 5 2 * * /home/u/bin/nightly-backup.sh >> /tmp/b.log 2>&1" in tab.read_text()
    assert console.perform("cron", "nightly-backup", "off", "o")[0] == 200
    assert "#agent-console-off# 30 5 2 * * /home/u/bin/nightly-backup.sh" in tab.read_text()
    assert console.perform("cron", "nightly-backup", "on", "o")[0] == 200
    lines = tab.read_text().splitlines()
    assert lines[0] == "# m h dom mon dow command" and lines[2].startswith("#0 3 * * *")   # others untouched
    assert lines[1].startswith("30 5 2 * * ")


def test_cron_leaves_lines_commented_by_hand_alone(console, site):
    before = site["crontab"].read_text()
    code, body = console.perform("cron", "disabled", "on", "o")
    assert code == 409 and "by hand" in body["error"]
    assert site["crontab"].read_text() == before


def test_one_action_at_a_time(console):
    console._busy.acquire()
    try:
        assert console.perform("restart", "agent-runner", "", "o")[0] == 409
    finally:
        console._busy.release()


def test_available_reports_what_the_host_allows(console):
    av = console.available()
    assert av["restart"] and av["cron"] and av["backend"]
    assert av["pause"]                   # the fake supervisor has CONTROL_DIR
    assert av["role-model"]              # template writable, restart allowed (fake sudo)
