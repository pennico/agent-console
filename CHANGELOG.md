# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-25

- **New layout, after media-library.**
  - A shaded header with search, unlock, Ask Claude and Account.
  - A hideable left pane with the view switch (Agents | Schedules |
    Connections), the Agents modes, and the groups with counts.
  - A toolbar with status chips (All, Running, Needs a look, Off), a filter
    box and a Cards | List switch.
  - Items as cards or a list.
  - A detail pane on the right that becomes a bottom sheet on phones;
    Escape or ✕ closes it.
  - The warm palette in light and dark mode.

## [0.1.0] - 2026-10-24

First release.

- **Agents view**, with two modes.
  - *By location*: containers, project workers, overseer, Telegram bridge,
    memory service, job bot, Claude Code Remote Control and chats, review
    timers.
  - *Roles*: a projects × roles grid and a list of the roles, with a
    "Model for this role" control.
- **Schedules view**: a 7-day timeline and a list grouped by cadence, with
  change and stop controls for worker ticks, GitHub Actions crons, host cron
  jobs, systemd timers and the job bot.
- **Connections view**: credentials and services from the private catalog,
  plus every Infisical folder with its key names and render state.
- **Lifecycle**: a "new" tag for things first seen this week, a
  Decommissioned group for things that are off, and a History group for
  removed things (`ledger.json`).
- **Search** across all views, including Infisical key names. Hover hints
  for models, cadences, connections and status. Clickable header counts.
- **Ask Claude** button: copies your question plus the selected item's
  context and opens Claude (no server-side chat).
- **Account** box: login, session, passkeys, host facts (OS, uptime, load,
  memory, disks, containers), version and your links.
- **Actions** through a host runner with a fixed allow-list:
  - restart, stop or start a container;
  - pause or resume a worker, or stop its current session;
  - run the overseer now;
  - switch the backend mode, or pin a model per role;
  - change the job bot's run time or mail interval;
  - reschedule, pause or resume a host cron job;
  - re-render the Infisical templates.
- **Console terminal** (ttyd + `term.py`) for changes that need sudo:
  - run a review timer now;
  - turn a timer on or off;
  - change a timer's calendar;
  - accept the current security state.
- **Unlock** with a password (scrypt) or a passkey (Touch ID / Face ID).
  Sessions last 30 minutes; five failures block unlocking for 10 minutes.
  Every action is audited.
- **Site specifics** (paths, names, descriptions, links, connections) come
  from `console.toml` and `catalog.json`; the code carries none.
- **Tests:**
  - runner allow-list;
  - web side as real processes;
  - passkeys with a software authenticator;
  - console terminal;
  - collector on a fake server, including a check that no secret value
    leaks;
  - page smoke test.
- **CI:** lint, tests on Python 3.11 and 3.13, image build, and a scan for
  private patterns. Tagged releases are drafted by a workflow.

[0.1.0]: https://github.com/pennico/agent-console/releases/tag/v0.1.0
