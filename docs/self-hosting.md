# Self-hosting on a tailnet device

Written for a Linux box on your tailnet with rootless Podman, `systemd --user` and Tailscale HTTPS certificates enabled (e.g.
`blend`). Two optional services: the **ledger server** (hardens blind review and approvals) and **ntfy** (phone notifications).
Nothing here runs on your Mac. **What was tested and what wasn't** is marked on each section.

Both follow the same pattern: the service listens on `127.0.0.1` only, and `tailscale serve` publishes it to your tailnet over HTTPS
with a real certificate, so nothing is exposed to your LAN or the internet and every client is a device on your tailnet.

---

## 1. The ledger server

**Do you need a container? No.** It is one stdlib Python file (the `hyra.pyz` bundle) and one SQLite file. A container adds an image
to maintain and no protection: the thing you're guarding against is an agent on your *Mac*, and the ledger is already on another
machine, reachable only with a token. A `systemd --user` service is the simplest thing that restarts on failure and survives reboots.
(A Quadlet container works too; see the end of this section.)

**Does it need an endpoint?** It has one: an HTTP API. You give it a stable HTTPS address with `tailscale serve`, below.

*Tested: the bundle runs on Python 3.14 here and the ledger worked end to end over the tailnet (omp worker + reviewer, Codex).
The systemd unit and `tailscale serve` lines below are written from the docs and the probes of `blend`, not run.*

### Install and start

```bash
# on your Mac, from the repo: copy the self-contained bundle
scp skills/hyra/scripts/hyra.pyz blend:~/hyra.pyz

# on blend:
mkdir -p ~/.config/systemd/user ~/.hyra-server && chmod 700 ~/.hyra-server
cat > ~/.config/systemd/user/hyra-ledger.service <<'EOF'
[Unit]
Description=Hyra ledger server
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 %h/hyra.pyz server serve --data %h/.hyra-server --host 127.0.0.1 --port 7421 --no-print-tokens
Restart=on-failure
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now hyra-ledger
loginctl show-user $USER -p Linger        # Linger=yes means it runs without you logged in (already yes on blend)
```

`--no-print-tokens` keeps the first-run tokens out of the journal. Read them once from the file, store them in your password manager,
and delete it:

```bash
cat ~/.hyra-server/initial-tokens.txt     # HYRA_HUMAN_TOKEN=...  HYRA_RUNNER_TOKEN=...
shred -u ~/.hyra-server/initial-tokens.txt
```

### Publish it on the tailnet

```bash
tailscale serve --bg --https=7421 http://127.0.0.1:7421
tailscale serve status                     # shows https://blend.<your-tailnet>.ts.net:7421 -> 127.0.0.1:7421
curl https://blend.<your-tailnet>.ts.net:7421/v1/health      # {"ok": true}   (run from your Mac)
```
To stop publishing: `tailscale serve --https=7421 off`. Restrict *which* of your devices can reach it with Tailscale ACLs if the
tailnet has other people's devices.

### Use it from a project (on your Mac)

```bash
cd my-research-repo
hyra server connect --url https://blend.<your-tailnet>.ts.net:7421
export HYRA_RUNNER_TOKEN=...                # only in the terminal where you start the scheduler
hyra run
hyra approval grant 3                        # prompts for the human token (type it; don't export it)
```

### Operating it
- **Rotate tokens** (lost or leaked): `systemctl --user stop hyra-ledger`, then run it once with
  `python3 ~/hyra.pyz server serve --data ~/.hyra-server --no-print-tokens --rotate-tokens` (Ctrl-C after it says "Rotated"), read the
  new file, delete it, `systemctl --user start hyra-ledger`. Old human/runner tokens stop working; running agents' session tokens
  keep working until they finish.
- **Logs:** `journalctl --user -u hyra-ledger -f`. **Audit trail** (who sealed/read/revealed/decided what):
  `sqlite3 ~/.hyra-server/ledger.db "select * from audit order by rowid desc limit 30"`.
- **Back up** `~/.hyra-server/ledger.db`. Losing it loses sealed claims (the projects keep everything else).
- **Update:** `scp` the new `hyra.pyz` and `systemctl --user restart hyra-ledger`.

### Optional: as a Quadlet container instead
`~/.config/containers/systemd/hyra-ledger.container`:
```ini
[Unit]
Description=Hyra ledger server

[Container]
Image=docker.io/library/python:3.12-slim
Exec=python /app/hyra.pyz server serve --data /data --host 0.0.0.0 --port 7421 --no-print-tokens
PublishPort=127.0.0.1:7421:7421
Volume=%h/hyra.pyz:/app/hyra.pyz:ro,Z
Volume=%h/.hyra-server:/data:Z
NoNewPrivileges=true
ReadOnly=true

[Service]
Restart=always

[Install]
WantedBy=default.target
```
then `systemctl --user daemon-reload && systemctl --user start hyra-ledger`. (Inside the container it binds `0.0.0.0` because the
published port is already pinned to `127.0.0.1` on the host.) Untested.

---

## 2. ntfy for phone notifications

`notify_cmd` runs a command whenever a message for you arrives; ntfy turns that into a push notification. Self-hosting means the
message text never leaves your tailnet.

**Two things to know first** (from ntfy's docs):
- **iOS:** instant delivery needs `upstream-base-url: https://ntfy.sh`. ntfy.sh then receives only a poll request with a message ID
  and a hash of the topic URL, never the text; the app then fetches the text from *your* server, so the phone must be connected to
  Tailscale. Without it, iOS notifications "can take hours".
- **Android:** the app keeps its own connection to your server, so no relay is needed; it also needs to reach your tailnet.

*Untested: this is written from ntfy's documentation and the probes of `blend` (package `ntfysh` exists in chaotic-aur; Podman 6.2;
Quadlet directory exists). I have not started it.*

### Run it (Quadlet)

`~/.config/containers/systemd/ntfy.container` (change the hostname to yours):
```ini
[Unit]
Description=ntfy push server

[Container]
ContainerName=ntfy
Image=docker.io/binwiederhier/ntfy:latest
Exec=serve
PublishPort=127.0.0.1:2586:80
Volume=%h/ntfy:/var/lib/ntfy:Z
Environment=NTFY_BASE_URL=https://blend.<your-tailnet>.ts.net:8443
Environment=NTFY_BEHIND_PROXY=true
Environment=NTFY_CACHE_FILE=/var/lib/ntfy/cache.db
Environment=NTFY_AUTH_FILE=/var/lib/ntfy/auth.db
Environment=NTFY_AUTH_DEFAULT_ACCESS=deny-all
# iOS only (drop this line if you only use Android):
Environment=NTFY_UPSTREAM_BASE_URL=https://ntfy.sh

[Service]
Restart=always

[Install]
WantedBy=default.target
```
```bash
mkdir -p ~/ntfy
systemctl --user daemon-reload && systemctl --user start ntfy
tailscale serve --bg --https=8443 http://127.0.0.1:2586
```

### Create a user for the phone and one that can only publish

```bash
podman exec -it ntfy ntfy user add phone                # prompts for a password
podman exec -it ntfy ntfy user add publisher
podman exec -it ntfy ntfy access phone hyra read-only
podman exec -it ntfy ntfy access publisher hyra write-only
podman exec -it ntfy ntfy token add --label=hyra publisher     # prints tk_...  (this is what hyra uses)
```
The publisher can only write to the topic `hyra`, so a leaked token can't read your notifications.

### Phone
Install the ntfy app, keep the Tailscale app connected, add the server `https://blend.<your-tailnet>.ts.net:8443`
(Settings → Default server, or "Use another server" when subscribing), log in as `phone`, subscribe to the topic `hyra`.

### Hook it up to hyra
Keep the token out of the project directory (agents can read it): export it in the shell where you start the scheduler and
reference it by name.
```bash
export NTFY_TOKEN=tk_...
hyra config set notify_cmd 'curl -s -H "Authorization: Bearer $NTFY_TOKEN" -d "$HYRA_NOTIFY_TEXT" https://blend.<your-tailnet>.ts.net:8443/hyra'
hyra msg send --to human --subject "test" --body "hello"      # the next `hyra run` tick pings you
```
Test the server by itself first:
`curl -H "Authorization: Bearer $NTFY_TOKEN" -d "it works" https://blend.<your-tailnet>.ts.net:8443/hyra`.

**Don't want to run anything?** Use `https://ntfy.sh/<a-long-random-topic>` with no auth: the topic name is the only secret, and the
text passes through ntfy.sh's server.
