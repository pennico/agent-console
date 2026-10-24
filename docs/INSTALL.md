# Install

Run each step yourself on the server, as the user that owns your agents
(the **console user**: it needs `docker` without sudo and can read the
runner's project folders). The commands use the default paths from
[`examples/console.example.toml`](../examples/console.example.toml). If you
change a path in the config, change it here too.

Nothing below opens a port to the internet. The console listens on
`127.0.0.1`, and Tailscale Serve publishes it on your tailnet only.

## 1. Code

```bash
sudo install -d -o "$USER" -g "$USER" -m 750 /opt/agent-console
curl -L https://github.com/pennico/agent-console/releases/download/v0.1.0/agent-console-0.1.0.tar.gz \
  | tar -xz --strip-components=1 -C /opt/agent-console      # or: git clone https://github.com/pennico/agent-console /opt/agent-console
cd /opt/agent-console
cp console/agent-console.py console/agent-status.py console/term.py deploy/Dockerfile deploy/docker-compose.yml .
```

## 2. Config, owner login and password

```bash
sudo install -d -m 750 -o root -g "$USER" /etc/agent-console
sudo install -m 640 -o root -g "$USER" examples/console.example.toml /etc/agent-console/console.toml
sudoedit /etc/agent-console/console.toml           # paths, container names, timezone, cron jobs, timers
# optional: descriptions, links and connections for your site
sudo install -m 640 -o root -g "$USER" examples/catalog.example.json /etc/agent-console/catalog.json

# Your tailnet login (the console accepts actions only from it) and the console's address:
tailscale status --json | python3 -c 'import json,sys; [print(u["LoginName"]) for u in json.load(sys.stdin)["User"].values()]'
HOST=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
printf 'OWNER_LOGIN=%s\nCONSOLE_ORIGIN=https://%s:8097\nCONSOLE_TERM_PUBLIC=8098\n' "<your login>" "$HOST" \
  | sudo tee /etc/agent-console/env >/dev/null

sudo python3 /opt/agent-console/agent-console.py passwd /etc/agent-console/password   # 12+ characters; prompts twice
sudo chgrp "$USER" /etc/agent-console/env /etc/agent-console/password
sudo chmod 640 /etc/agent-console/env /etc/agent-console/password
```

Keep the password in your password manager. The file holds only an scrypt
hash.

## 3. Collector and page

```bash
sudo install -d -o "$USER" -g "$USER" -m 755 /var/lib/agent-console /var/lib/agent-console/web
install -d -m 700 /var/lib/agent-console/run /var/lib/agent-console/state
install -m 644 console/index.html /var/lib/agent-console/web/
python3 /opt/agent-console/agent-status.py && python3 -m json.tool /var/lib/agent-console/web/status.json | head -40
(crontab -l 2>/dev/null; echo "* * * * * flock -n /tmp/agent-status.lock python3 /opt/agent-console/agent-status.py 2>> /var/lib/agent-console/agent-status.err") | crontab -
```

Check the first `status.json`: your workers, containers and timers should be
there. Anything unreadable shows as `unknown`. In that case the console user
can't read that source; fix it in the config or with file permissions.

## 4. Action runner

```bash
sudo install -m 644 deploy/systemd/agent-console-runner@.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now "agent-console-runner@$USER"
systemctl is-active "agent-console-runner@$USER" && ls -l /var/lib/agent-console/run/
```

## 5. Web container

```bash
cp deploy/env.example .env
sed -i "s/^CONSOLE_UID=.*/CONSOLE_UID=$(id -u)/; s/^CONSOLE_GID=.*/CONSOLE_GID=$(id -g)/" .env
ss -ltn | grep -q '127.0.0.1:8097 ' && echo "8097 is taken: set CONSOLE_PORT in .env"
docker compose up -d --build && docker compose ps
```

## 6. Console terminal (optional)

Needed only for the "⌨" buttons: changes that need sudo, such as systemd
timers.

```bash
sudo apt install -y ttyd        # Debian 12, Debian 14+, Ubuntu 22.04+
# Debian 13 has no ttyd package. Use the project's static build instead, and check it
# against the SHA256SUMS file published on the same release page:
#   curl -fLO https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64
#   sha256sum ttyd.x86_64          # must equal the ttyd.x86_64 line in SHA256SUMS
#   sudo install -m 755 ttyd.x86_64 /usr/local/bin/ttyd
sed "s#/usr/bin/ttyd#$(command -v ttyd)#" deploy/systemd/agent-console-term@.service \
  | sudo tee /etc/systemd/system/agent-console-term@.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now "agent-console-term@$USER"
```

## 7. Publish on your tailnet

```bash
sudo tailscale serve --bg --https=8097 http://127.0.0.1:8097
sudo tailscale serve --bg --https=8098 http://127.0.0.1:8098    # only with the console terminal
```

Open `https://<server>.<tailnet>.ts.net:8097/`. If you publish on other
ports, put them in `/etc/agent-console/env` (`CONSOLE_ORIGIN`,
`CONSOLE_TERM_PUBLIC`) and run `docker compose up -d`.

## Verify

1. Unlock with the password. The header should show "Unlocked · locks in 30
   min". Then add Touch ID / Face ID under Connections → Console access,
   press Lock, and unlock with it.
2. Use any safe action, for example "Run overseer now". Then check
   `tail /var/lib/agent-console/actions.log`.
3. Use a "⌨" action and answer `n`. The terminal should say "Nothing changed".

## Which buttons need what

- **Container, worker, overseer and cron buttons** run as the console user;
  nothing extra is needed.
- **Infisical restart buttons** restart the Infisical agent with `sudo -n`
  (the `[infisical] restart` command). They show as unavailable until the
  console user may run that one command without a password, and they are:
  - "Re-render now";
  - "Model for this role";
  - the job bot's run time and mail interval.

  The rule would look like this:

  ```
  <user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart infisical-agent
  ```

  Add it only if you accept that anyone with the console user's shell can
  run that command too.
- **The backend switch** runs your `[runner] mode_command` script and is
  available when that file is executable.
- **Review-timer buttons (⌨)** open the console terminal, where sudo asks
  for your password. No rule is needed.

## Update

```bash
cd /opt/agent-console
curl -L https://github.com/pennico/agent-console/releases/download/vX.Y.Z/agent-console-X.Y.Z.tar.gz | tar -xz --strip-components=1
cp console/agent-console.py console/agent-status.py console/term.py deploy/Dockerfile deploy/docker-compose.yml .
install -m 644 console/index.html /var/lib/agent-console/web/
sudo systemctl restart "agent-console-runner@$USER" "agent-console-term@$USER"
docker compose up -d --build
```

## Uninstall

```bash
sudo tailscale serve --https=8097 off; sudo tailscale serve --https=8098 off
cd /opt/agent-console && docker compose down && docker image rm agent-console:local
sudo systemctl disable --now "agent-console-runner@$USER" "agent-console-term@$USER"
sudo rm -f /etc/systemd/system/agent-console-runner@.service /etc/systemd/system/agent-console-term@.service
sudo rm -f /etc/systemd/system/*.timer.d/agent-console.conf && sudo systemctl daemon-reload   # calendar changes made from the console
crontab -l | grep -v agent-status.py | crontab -
sudo rm -rf /etc/agent-console /opt/agent-console /var/lib/agent-console
```

A cron line the console stopped starts with `#agent-console-off# `. Remove
that marker by hand to re-enable the line.
