# agent-console

A private web console for the AI agents running on a home server: what is
running, when each thing runs next, which credentials each agent uses, and a
small set of safe actions (restart, pause a worker, change a schedule).
Opened from a phone or laptop over Tailscale; nothing is exposed to the
internet.

It was built for one setup: an autonomous coding loop that runs one worker
per GitHub project in a Docker container, plus a few other agents next to it.
The parts it reads are listed under [What it reads](#what-it-reads), and
anything it can't find shows as unknown rather than failing. Site details
(paths, names, descriptions, links) live in two private files
([configuration](docs/CONFIGURATION.md)); the code itself carries none.

## What you get

Three views, switched with the tabs at the top, plus search (`/`) across all
of them:

- **Agents**, in two modes:
  - *By location*: grouped by where each agent runs (the runner container
    with its project workers, overseer, Telegram bridge and memory service; a
    job-search bot; a Claude Code container with Remote Control and its chats;
    review timers on the host). Each item has its status, schedule, model,
    connections, recent sessions and actions.
  - *Roles*: a projects × roles grid (sessions this week / total, shaded by
    activity, marks for running and failed) above the role list. Each role
    has its model, limits and recent sessions, plus a control that pins the
    role to a model.
- **Schedules**: a 7-day timeline (a dot per run, hollow when a run may not
  fire) above a list grouped by cadence. It covers worker ticks, GitHub
  Actions crons, host cron jobs, systemd timers and the bot's daily run.
  Every schedule has its change and stop controls.
- **Connections**: Telegram bots, Google APIs, GitHub tokens, model
  providers and other services, from your catalog. Each one lists who uses
  it, the credential's *name* and where it is stored, expiry, how to rotate
  it, and links. It also lists the Infisical folders with every key name and
  whether that key reached the rendered file.

Across the page:

- A **new** tag marks things first seen this week.
- Things that are on the server but off move to a **Decommissioned** group.
- Things that were removed are kept, greyed out, under **History**.
- Hover hints show the exact model behind a model pill and explain each tag.
- The header counts ("2 running", "1 needs a look") open the matching items.
- **Ask Claude** (top right) turns your question and what the console shows
  about the selected item into a prompt. It copies the prompt and opens Claude,
  where your Remote Control session lives. The console itself never runs Claude.
- **Account** (top right) shows who is signed in, the session and passkeys,
  facts about the server (OS, uptime, load, memory, disks, containers), the
  console's version, and your links.

## How it works

```
 host (the console user)                                  container (unprivileged)
 ┌───────────────────────────────┐   status.json   ┌────────────────────────────┐
 │ agent-status.py  (cron, 1/min)│ ──────────────▶ │ agent-console.py serve     │◀── Tailscale Serve
 │   docker inspect/top/logs,    │                 │   page + status, password, │    (https, tailnet only)
 │   project logs, crontab,      │                 │   passkeys, 30-min session │
 │   systemctl, Infisical names  │                 └─────────────┬──────────────┘
 │                               │   Unix socket (owner-only)    │ action request
 │ agent-console.py runner ◀─────┼───────────────────────────────┘
 │   fixed allow-list, one at a  │
 │   time, audit log             │
 │                               │
 │ ttyd → term.py  (optional) ◀──┼── "⌨" buttons open it with the change prefilled;
 │   changes that need sudo      │    it shows the commands, asks y/N, sudo asks your password
 └───────────────────────────────┘
```

- **Collector** (`console/agent-status.py`) runs every minute as a normal
  user and only reads. It writes `status.json` and `ledger.json`, the
  first-seen/removed history behind the lifecycle groups.
- **Web side** (`agent-console.py serve`) runs in a read-only container
  with no capabilities and no Docker socket. It serves the page and handles
  unlocking, then hands each action to the runner.
- **Runner** (`agent-console.py runner`) is a host service. It re-checks
  every request against a fixed list of actions, targets and value patterns,
  runs one at a time and logs each one. Nothing typed in the page reaches a
  shell.
- **Console terminal** (`term.py` under ttyd, optional) handles changes that
  need sudo, such as systemd timers. It shows the exact commands, asks for
  confirmation, and sudo asks for your password there. No passwordless sudo
  is needed.

See [SECURITY.md](SECURITY.md) for the full security model.

## What it reads

Every source is optional. Missing ones show as "unknown".

| Source | Expected shape |
|---|---|
| Runner container | a supervisor (`supervisor.py` with `PROJECT_REPOS = [...]`) that starts one worker per project; project checkouts under `projects/<repo>/`; optional pause flags `projects/.control/pause-<repo>` |
| Dispatcher logs | `projects/<repo>/dispatcher/logs/<YYYYmmdd-HHMMSS>-<backend>-<role>.{json,txt,exit}`; `dispatcher/state.json` for blockers, quota pauses and roadmap state |
| Runner settings | `AGENT_BACKEND`, `AGENT_BACKEND_<ROLE>` and `DISPATCH_*` limits from the runner's `.env` (only those keys are read) |
| GitHub Actions | `schedule:` crons in each project's `.github/workflows/*.yml`, with an optional `vars.<NAME> == 'enabled'` gate |
| Job bot container | `RUN_AT` and `EMAIL_CHECK_INTERVAL_S` from its `.env`; run lines from `docker logs` |
| Claude Code container | `remote-control` in its process list; chat transcripts (`*.jsonl`) for titles and activity |
| Host cron | the console user's crontab lines you list in the config |
| systemd timers | review timers named `<prefix><tier>.timer` |
| Infisical | templates (`*.tmpl`) read for key **names** only; the agent config for where each renders |

## Install

Requirements:

- Linux with Docker and Python 3.11+ on the host.
- Tailscale with Serve (or another authenticating proxy that sets a login
  header).
- `ttyd`, only for the optional console terminal.

The step-by-step guide is [docs/INSTALL.md](docs/INSTALL.md). The short
version:

1. Put the code in `/opt/agent-console`.
2. Write `/etc/agent-console/console.toml` (start from
   [the example](examples/console.example.toml)) and optionally
   `catalog.json` ([example](examples/catalog.example.json)).
3. Set a password with `agent-console.py passwd`.
4. Add the collector to cron.
5. Start the runner unit and `docker compose up -d --build`.
6. Publish the console with `tailscale serve`.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                    # runner, web side, passkeys, terminal, collector, page smoke test (needs node)
ruff check .
scripts/scan-private.sh      # no private patterns in tracked files
```

The tests build a fake server in a temporary folder: fake `docker`,
`crontab`, `sudo` and `systemctl` commands, project logs, Infisical
templates with dummy values. They check:

- the runner refuses anything off its list;
- the web side, run as real processes, enforces the owner login, the
  password, sessions, cross-site checks and lockout;
- passkeys work with a software authenticator, and replays, forged
  signatures and missing user verification are refused;
- no planted value ever reaches `status.json`;
- the page renders every view from the collector's output.

`console/index.html` is a single file with no build step and no external
requests.

## License

[MIT](LICENSE)
