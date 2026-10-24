# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub's private
vulnerability reporting:

- go to the [Security tab → "Report a vulnerability"](https://github.com/pennico/agent-console/security/advisories/new),
  fill in the form, and submit. Only the maintainer can see the report.

Please do not open a public issue with security details. If the form is
unavailable for any reason, open a public issue that only says you have
a security report and asks for a private channel, without details. The
maintainer will follow up.

You can expect an acknowledgement within a week. Fixes are released on
`main` and as a new tagged release; there are no maintained release
branches.

## Security model

The console can restart containers and change schedules on your server, so
it is built to be reachable only from your own devices and to do only what
is on a fixed list.

**Who can see it.** Anyone on your tailnet who can reach the published
port can see status. `status.json` holds no secret values, but it does
include chat titles, issue numbers and your catalog's descriptions.

**Who can act.** Every action needs all of these:

- **Owner login.** The `Tailscale-User-Login` header that Tailscale Serve
  adds must equal `OWNER_LOGIN`.
- **A live session**, from either of:
  - the **password**: an scrypt hash in a root-owned file, never stored by
    the page;
  - a **passkey** (Touch ID / Face ID; WebAuthn ES256) added while
    unlocked. Only its public key is stored. Each login checks the origin,
    a single-use challenge, the domain hash, user verification, the
    signature and the signature counter.
- **The session cookie** (`__Host-` prefix, HttpOnly, Secure,
  SameSite=Strict) ends after 30 minutes.
- **A custom `X-Console-Action` header**, which another site can't send
  without a CORS preflight that the server never answers.
- **A same-origin `Origin`.**

Five failed unlocks block unlocking for 10 minutes. Every unlock and action
is written to the audit log.

**What it can do.** The web side runs in a container with:

- a read-only filesystem;
- no capabilities and no Docker socket;
- the console user's uid.

It can only ask the host runner, over an owner-only Unix socket, to perform
a named action. The runner re-checks the action, target and value against
its own list:

- targets come from fixed lists or from the supervisor's own project list;
- values must match patterns such as `HH:MM`, a 5-field cron of digits and
  `*/,-`, or a systemd calendar of a limited character set.

Commands are fixed argument lists, never a shell. File edits replace exactly
one `KEY=value` line in an Infisical template, or one known line in the
console user's crontab.

**Changes that need sudo** go through the console terminal: ttyd on
loopback, published by Tailscale Serve, requiring the login header. It runs
`term.py`, which has its own allow-list. It prints every command, asks for
confirmation, and leaves the password prompt to sudo. The password is typed
there and never stored. No passwordless sudo rules are required.

**Secrets.** The collector reads Infisical templates for key names only.
Secret references never carry values, and plain settings show a value only
when they are on a short list of known non-secret settings. For the rendered
`.env` files it records only whether each secret line is non-empty.

## In scope

- bypassing the owner, session, Origin or header checks;
- making the runner or `term.py` run something off their lists, or with a
  value their patterns should refuse;
- secret values reaching `status.json`, `ledger.json`, the audit log or the
  page;
- script injection through data that the page renders, such as chat
  titles, issue text or catalog entries;
- passkey verification flaws.
