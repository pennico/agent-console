# Configuration

Two files, both outside the code and both free of secret values:

- **`/etc/agent-console/console.toml`**: where things are on this server
  (paths, container names, timezone, cron jobs, timers). Every key is
  optional; the defaults are generic. Start from
  [`examples/console.example.toml`](../examples/console.example.toml), which
  documents each key. Override the file's location with
  `AGENT_CONSOLE_CONFIG`.
- **`/etc/agent-console/catalog.json`** (`[site] catalog`): how you want
  things described: descriptions, links, connections, model notes. The
  collector copies it into `status.json`, so it must contain only what the
  people on your tailnet may see. Start from
  [`examples/catalog.example.json`](../examples/catalog.example.json).

## console.toml sections

| Section | Used by | What for |
|---|---|---|
| `[site]` | all | `timezone` (dispatcher log names and job times use it), `github_owner` (repo links), `catalog` path, `disks` (mount points shown in the Account box) |
| `[paths]` | collector, page | `web`: where `index.html`, `status.json` and `ledger.json` live; `install`: shown in commands |
| `[console]` | web side, runner, terminal | runner socket, passkey file, audit log, password file, collector path (run after each action) |
| `[runner]` | collector, runner | **optional: leave the section out on a site without the agent loop** (see below). The runner's checkout (`dir`, `projects`), container name, its Infisical template name, the project that runs the overseer (`ops_repo`), the backend switch script (`mode_command`), `app_decommissioned`, `[runner.activity]` evidence files, `bridge_container` + `bridge_state` when the Telegram bridge runs in its own container |
| `[jobbot]` | collector, runner | container, `.env`, template name, `run_window` that blocks manual runs next to the scheduled one, `mail_monitor` / `chat` (false hides those parts), `last_run_file` (its mtime = last run when the log has none) |
| `[claude]` | collector | container name and the transcripts folder |
| `[infisical]` | collector, runner | templates folder, agent config, unit name, the restart command, extra setting names whose values may be shown |
| `[overwatch]` | collector, runner, terminal | timer tiers and the unit prefix (`<prefix><tier>.timer`) |
| `[cron.<id>]` | collector, runner | one table per host cron job: `match` (text in the crontab line), `project`, `label` |

### A site without the agent loop

The agent loop (the runner container with its project workers, overseer,
Telegram bridge and memory service) is optional. Whether a site has it is
decided by one thing: **is there a `[runner]` table in `console.toml`?**

- **Section present** (even empty): the loop is on, with the defaults for any
  key you leave out. Existing configs keep working unchanged.
- **Section absent** (no `[runner]` and no `[runner.activity]`): the collector
  writes `"agent_runner": null` to `status.json` and never reads the runner's
  folders or its container. The page leaves out the runner section, its
  workers, overseer, bridge and memory, the Roles view, backend and mode tags
  and the loop's schedules (worker ticks, overseer, bridge, project GitHub
  Actions crons). The loop's actions (`overseer-now`, `pause`, `resume`,
  `stop-session`, `role-model`, `backend`) answer "not configured", and the
  runner container is no longer a target for restart, stop or start.

Host cron jobs (`[cron.<id>]`) are not part of the loop: they stay in
both cases (in `status.json` they are now the top-level `cron` object). A
cron job whose `project` has no worker shows in the Schedules view only.
Drop the catalog entries that describe the loop (`agent-runner`, `w-<repo>`,
`overseer`, `bridge`, `memory`, `role-<key>`) when you remove the section;
entries for items that don't exist are ignored either way.

The host action runner (`agent-console.py runner`, `[console] runner_sock`)
is a different thing with a similar name: it runs every console action and
is needed with or without the agent loop.

Environment variables override a few paths. This is how the container finds
things at its own mount points:

| Variable | Overrides |
|---|---|
| `CONSOLE_WEB` | `[paths] web` |
| `CONSOLE_RUNNER_SOCK` | `[console] runner_sock` |
| `CONSOLE_PASSWORD_FILE` | `[console] password_file` |
| `CONSOLE_PASSKEYS` | `[console] passkeys` |
| `CONSOLE_LOG` | `[console] log` |

The web side alone also reads:

- `OWNER_LOGIN` (required): your tailnet login;
- `CONSOLE_ORIGIN`: the console's https URL, used for the Origin check and
  as the passkey domain;
- `CONSOLE_TERM_PUBLIC`: the published terminal port;
- `CONSOLE_COOKIE_SECURE=0`: for plain-http tests only.

## Item ids

The catalog refers to items by id:

| Id | Item |
|---|---|
| `agent-runner`, `job-bot`, `claude-agent` | the three containers (ids stay fixed even if you rename the containers; `agent-runner` only with a `[runner]` section) |
| `w-<repo>` | a project worker (agent loop) |
| `overseer`, `bridge`, `memory` | the runner's other processes (agent loop) |
| `gmail`, `jb-chat` | the job bot's mail monitor and chat |
| `chat-<uuid>` | a Claude chat |
| `ow-<tier>`, `ow-bot` | a review timer and its bot |
| `role-<key>` | a role: `impl`, `arch`, `qa`, `adv`, `verify`, `overseer`, `chat` (agent loop) |
| `sch-rc`, `sch-bridge`, `sch-jbchat` | always-on schedules (`sch-bridge` with the agent loop) |
| `sch-ticks`, `sch-gmail`, `sch-jobbot`, `sch-overseer` | the other fixed schedules (`sch-ticks`, `sch-overseer` with the agent loop) |
| `sch-wf-<repo>-<workflow>` | a GitHub Actions cron |
| `sch-ow-<tier>` | a review timer's schedule |
| `sch-cron-<id>` | a host cron job from `[cron.<id>]` |
| `cn-<key>` | a connection from the catalog |
| `cn-inf-<template>` | an Infisical folder |
| `cn-console` | the console's own access settings |

## catalog.json keys

| Key | Shape | Effect |
|---|---|---|
| `items` | `{id: {...}}` | per item: `name`, `sub`, `where`, `head`, `how` (list of HTML strings), `links` (replace), `extra_links` (append), `cmds` (`[label, command]` pairs), `schedule_src`, `tags` (merged; `connUrl` and `connHint` merge per name) |
| `connections` | list | the Connections view. Each entry has: `key`, `section`, `name`, `tags`, `users` (item ids), `cred` (a name, never a value), `stored`, `expiry`, `rotate`, `test`, `head`, `how`, `links`, `models` |
| `roles` | `{key: {sub, what, how}}` | your wording for a role |
| `links` | `{name: [title, url, note]}` | named links some items use (`infisical`, `botfather`, …) |
| `conn_url`, `conn_hint` | `{chip: …}` | where a connection chip links to and what its hint says |
| `project_conns`, `project_conn_url` | `{repo: …}` | the connection chips on a project worker |
| `model_hint`, `item_model_hint`, `item_model_label` | `{…}` | the exact model behind a model pill, in general or per item |
| `conn_inf`, `inf_users`, `inf_of_item`, `sch_inf` | `{…}` | which Infisical keys and folders belong to which item, when the defaults (template name = container or project name) don't fit |
| `rc_name`, `claude_model`, `agent_account`, `infisical_project` | strings | small bits of wording |
| `account_links` | list of `[title, url, note]` | the Links section of the Account box (default: Claude usage, Claude Code, your GitHub) |
| `ask_claude_url` | string | where "Ask Claude" opens (default `https://claude.ai/code`, where Remote Control sessions are listed) |

The catalog is shown to everyone who can open the console. Write credential
*names* and where they are stored, never values.
