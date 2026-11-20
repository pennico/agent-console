"""term.py: the console terminal's allow-list, the commands it shows, and the y/N gate."""

import subprocess
import sys

import pytest

from conftest import CONSOLE, calls


def term(*args, answer="n"):
    r = subprocess.run([sys.executable, str(CONSOLE / "term.py"), *args], input=answer + "\n\n",
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout


@pytest.mark.parametrize("args", [
    (), ("overwatch-run",), ("rm", "-rf"), ("overwatch-run", "hourly"), ("overwatch-run", "daily", "x"),
    ("overwatch-timer", "daily", "maybe"), ("overwatch-calendar", "daily", "07:00\nExecStart=/bin/sh"),
    ("overwatch-calendar", "daily", "$(id)"), ("overwatch-accept", "daily"),
    ("run",), ("run", "nope"), ("run", "upgrade", "--force"), ("run", "no-steps"), ("run", "Bad Name"),
])
def test_refuses_anything_off_the_list(site, args):
    code, out = term(*args, answer="y")
    assert code == 2
    assert "Refused" in out or "Unknown" in out
    assert calls(site) == []


def test_shows_the_commands_and_changes_nothing_when_declined(site):
    code, out = term("overwatch-calendar", "weekly", "Sun *-*-* 09:00:00", answer="n")
    assert code == 1 and "Nothing changed." in out
    assert "sudo tee /etc/systemd/system/overwatch-weekly.timer.d/agent-console.conf" in out
    assert "systemd-analyze calendar 'Sun *-*-* 09:00:00'" in out
    assert calls(site) == []


def test_runs_the_steps_when_confirmed(site):
    code, out = term("overwatch-timer", "daily", "off", answer="y")
    assert code == 0, out
    assert calls(site)[0] == "sudo systemctl disable --now overwatch-daily.timer"
    assert '"via": "terminal"' in site["log"].read_text()


def test_site_command_shows_preview_and_notes_then_asks(site):
    code, out = term("run", "upgrade", answer="n")
    assert code == 1 and "Nothing changed." in out
    assert "Install the waiting updates" in out and "PLAN: 2 packages" in out
    assert "Docker restarts every container" in out and "sudo apt-get upgrade" in out
    assert calls(site) == ["plan-updates "]          # only the read-only preview ran


def test_site_command_runs_when_confirmed(site):
    code, out = term("run", "upgrade", answer="y")
    assert code == 0, out
    assert calls(site) == ["plan-updates ", "sudo apt-get upgrade"]
    assert '"action": "run", "target": "upgrade"' in site["log"].read_text()


def test_failed_preview_stops_before_the_question(site):
    code, out = term("run", "bad-preview", answer="y")
    assert code != 0 and "preview failed" in out
    assert calls(site) == []
