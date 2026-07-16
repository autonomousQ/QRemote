```
 ██████╗  ██████╗  ███████╗ ███╗   ███╗  ██████╗  ████████╗ ███████╗
██╔═══██╗ ██╔══██╗ ██╔════╝ ████╗ ████║ ██╔═══██╗ ╚══██╔══╝ ██╔════╝
██║   ██║ ██████╔╝ █████╗   ██╔████╔██║ ██║   ██║    ██║    █████╗
██║▄▄ ██║ ██╔══██╗ ██╔══╝   ██║╚██╔╝██║ ██║   ██║    ██║    ██╔══╝
╚██████╔╝ ██║  ██╗ ███████╗ ██║ ╚═╝ ██║ ╚██████╔╝    ██║    ███████╗
 ╚══▀▀═╝  ╚═╝  ╚═╝ ╚══════╝ ╚═╝     ╚═╝  ╚═════╝     ╚═╝    ╚══════╝
Remote control your Windows PC via Telegram
```

![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python)
![Platform](https://img.shields.io/badge/platform-windows-0078d4?logo=windows)
![Build](https://img.shields.io/badge/build-passing-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

Remotely control your Windows PC from Telegram — anywhere in the world.

Sends commands through a **Cloudflare Tunnel** to a local **Flask webhook server**, which triggers privileged Windows **Task Scheduler** tasks so the server itself never needs to run as Administrator. Any task you create in Task Scheduler with a name starting with `Remote` is instantly available as a `/command` in the bot.

---

## Commands

| Command | Action |
|---------|--------|
| `/shutdown` | Schedules a shutdown in 60 seconds |
| `/reboot` | Schedules a reboot in 60 seconds |
| `/cancel` | Aborts the pending shutdown or reboot |
| `/status` | Confirms the bot is online |
| `/tasks` | Lists all `Remote*` Task Scheduler tasks and their bot commands |
| `/help` | Shows this command list |
| `/<name>` | Runs any `Remote<Name>` task you created (e.g. `/winver` → `RemoteWinver`) |

---

## Dynamic Task Scheduler Commands

Create a Task Scheduler task whose name begins with `Remote` and it becomes a bot command automatically — no code changes needed.

**Example:** create a task named `RemoteWinver` that runs `winver.exe`. Then in Telegram:

```
/winver        → runs RemoteWinver
/tasks         → lists all available Remote* tasks
```

The mapping is always: `/command` → `Remote` + `Command` (first letter capitalised).

---

## Quick Start

```powershell
# 1. Run setup (as Administrator)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup.ps1

# 2. Start the bot (tunnel + webhook registered automatically)
python server.py
```

You will receive a Telegram message — *"Bot online. Tunnel: https://xxx.trycloudflare.com"* — confirming the webhook is live.

See **[SETUP.md](SETUP.md)** for the full walkthrough, including named tunnel setup and auto-start on boot.

---

## Important: Run Setup as the Account That Stays Logged In

`setup.ps1` registers `RemoteShutdown`, `RemoteReboot`, and `QRemoteBotStart` to run as **whichever Windows account is running the script** (`$env:USERNAME`), not as SYSTEM. This means:

- **Run `setup.ps1` while logged in as the account you normally use day to day** — the same account that will be logged in when you want to control the PC remotely. If an admin/IT account sets this up for a different standard-user account, the tasks will be bound to the wrong user and `/shutdown`, `/reboot`, and other Remote* commands will fail with "Access is denied".
- **QRemoteBotStart only starts at logon**, so the bot (and its ability to trigger tasks) is only running while that account has an active session. If the PC reboots and nobody logs in, the bot won't come back online and you won't be able to reach it remotely — even to send `/status`.

### Enable auto-login so the bot survives a reboot

Since the bot depends on your account being logged in, set up passwordless auto-login so a reboot (including one triggered by `/reboot` itself) doesn't strand you without remote access:

```powershell
netplwiz
```

1. Uncheck **"Users must enter a user name and password to use this computer"**.
2. Click **Apply** — a credentials prompt appears.
3. Enter your account's password (or leave blank if the account has no password) and confirm.
4. Click **OK**.

Windows will now sign the account in automatically at boot, `QRemoteBotStart` fires, and the bot re-establishes its tunnel and Telegram webhook without you touching the keyboard.

---

## Tunnel Automation

When `server.py` starts it calls `start_tunnel()`, which automates the entire Cloudflare → Telegram wiring:

```
1. Spawn cloudflared
   └─ cloudflared tunnel --url http://localhost:5000
      Writes all output to cloudflared.log

2. Discover public URL
   └─ Polls cloudflared.log every second (up to 30 s)
      until a *.trycloudflare.com URL appears

3. Wait for DNS propagation
   └─ Calls socket.getaddrinfo() on the hostname
      Retries every second for up to 10 s

4. Register Telegram webhook
   └─ POST https://api.telegram.org/bot<TOKEN>/setWebhook
      Retries up to 20 times (5 s apart) until Telegram confirms

5. Notify you
   └─ Sends "Bot online. Tunnel: <url>" to your Telegram chat
```

On each restart a fresh `*.trycloudflare.com` URL is assigned and re-registered automatically — no manual webhook updates needed.

---

## Architecture

```
Telegram servers
      │  HTTPS POST /webhook
      ▼
Cloudflare Tunnel  ──►  localhost:5000  (server.py / Flask)
                                │
                    ┌───────────┴────────────────────┐
                    │  Route command to task          │
                    │  /winver → RemoteWinver         │
                    │  /shutdown → shutdown.exe       │
                    └───────────┬────────────────────┘
                                │ schtasks /run /tn …
                    ┌───────────┴──────────────────────────┐
                    ▼                ▼              ▼
             RemoteShutdown   RemoteReboot   RemoteWinver …
             (current user)   (current user) (current user)
```

---

## Files

```
C:\Projects\QRemote\
├── server.py               Flask webhook server
├── server.cmd              Launches server.py (python server.py)
├── server_hidden.vbs       Runs server.cmd with no console window (used by QRemoteBotStart)
├── setup.ps1               One-time setup script (run as Administrator)
├── requirements.txt        Python dependencies
├── .env                    Bot token + allowed user ID (created by setup.ps1)
├── .env.example            Template for .env
├── QRemote.log             JSON event log
├── cloudflared.log         Cloudflare Tunnel output (auto-created)
├── SETUP.md                Full setup guide
└── README.md               This file
```

---

## Security

- **Single-user auth** — only the Telegram user ID in `.env` can issue commands. All other senders are silently rejected and logged.
- **No admin Flask process** — the Flask server triggers pre-created Task Scheduler tasks via `schtasks /run` rather than calling privileged executables directly, so the server process needs no elevated privileges. Tasks run as the current user (who holds shutdown rights), so the non-elevated bot can trigger them — see [Run Setup as the Account That Stays Logged In](#important-run-setup-as-the-account-that-stays-logged-in).
- **Credentials never in code** — bot token and user ID live in `.env` (excluded from version control via `.gitignore`).
- **JSON audit log** — every accepted command and every rejected request is recorded with a UTC timestamp to `QRemote.log`.

---

## Requirements

- Windows 10 / 11
- Python 3.10+
- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
- A Telegram bot (created via [@BotFather](https://t.me/BotFather))

---

## Support

If you find this useful, consider buying me a coffee!

[![Donate via PayPal](https://img.shields.io/badge/Donate-PayPal-blue?logo=paypal)](https://paypal.me/tommykho)
