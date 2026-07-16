# Setup Guide — Remote Shutdown Bot

## Prerequisites

| Tool | Install |
|------|---------|
| Python 3.10+ | https://python.org/downloads |
| pip | bundled with Python |
| Cloudflare Tunnel (`cloudflared`) | `winget install Cloudflare.cloudflared` |
| A Telegram bot token | created via [@BotFather](https://t.me/BotFather) |
| Your Telegram user ID | obtained via [@userinfobot](https://t.me/userinfobot) |

---

## Step 1 — Create a Telegram Bot

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts to name your bot.
3. BotFather replies with a token like `123456789:ABCDEFGhijklmnopqrstuvwxyz`. **Save it.**

## Step 2 — Get Your Telegram User ID

1. Open Telegram and search for **@userinfobot**.
2. Send `/start`. It replies with your numeric user ID (e.g. `987654321`). **Save it.**

## Step 3 — Run the Setup Script

Open PowerShell **as Administrator** and run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup.ps1
```

The script will:
- Install Python requirements (`flask`, `requests`, `python-dotenv`)
- Create the **RemoteShutdown** scheduled task (runs `shutdown /s /t 60` as the current user)
- Create the **RemoteReboot** scheduled task (runs `shutdown /r /t 60` as the current user)
- Prompt you to enter your bot token and user ID, then write `.env`
- Create the log file at `C:\projects\telegram\remote_shutdown.log`

## Step 4 — Set Up Cloudflare Tunnel

### Option A — Quick Tunnel (temporary URL, no account needed)

```powershell
cloudflared tunnel --url http://localhost:5000
```

`cloudflared` prints a line like:
```
Your quick Tunnel has been created! Visit it at (it may take some time to be fully reachable):
https://random-words-here.trycloudflare.com
```

Copy that URL. It changes every time you restart cloudflared.

### Option B — Named Tunnel (permanent URL, requires Cloudflare account)

```powershell
# Authenticate (opens browser)
cloudflared tunnel login

# Create a named tunnel
cloudflared tunnel create remote-shutdown

# Route your domain (replace with your domain)
cloudflared tunnel route dns remote-shutdown shutdown.yourdomain.com

# Run the tunnel
cloudflared tunnel run remote-shutdown
```

The tunnel URL will be `https://shutdown.yourdomain.com`.

## Step 5 — Register the Telegram Webhook

Replace `<TOKEN>` and `<TUNNEL_URL>` with your values:

```powershell
Invoke-RestMethod "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<TUNNEL_URL>/webhook"
```

A successful response looks like:
```json
{"ok":true,"result":true,"description":"Webhook was set"}
```

Verify the webhook at any time:
```powershell
Invoke-RestMethod "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

## Step 6 — Start the Bot

```powershell
python C:\Projects\Telegram\server.py
```

Test it by sending `/status` to your bot in Telegram. You should receive **"Online."**

---

## Auto-Start on Boot (Optional)

To keep the bot running without manually starting it, create a Task Scheduler task:

```powershell
# Run as Administrator
$action = New-ScheduledTaskAction `
    -Execute "python.exe" `
    -Argument "C:\Projects\Telegram\server.py" `
    -WorkingDirectory "C:\Projects\Telegram"

$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName  "RemoteShutdownBot" `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -RunLevel  Highest `
    -Force
```

---

## Troubleshooting

### "schtasks /run failed" in the bot log
The `RemoteShutdown` or `RemoteReboot` task may not exist. Re-run `setup.ps1` as Administrator to recreate them.

### Telegram webhook returns `{"ok":false}`
- Confirm the tunnel URL is reachable in a browser (it should return a 404 or empty response on `/`).
- Make sure the URL ends in `/webhook`, not just the domain root.

### Bot doesn't respond
- Check that `server.py` is running and not errored out.
- Check the log file at `C:\projects\telegram\remote_shutdown.log`.
- Confirm `getWebhookInfo` still shows your current tunnel URL (quick tunnels change on restart).

### `/cancel` reports "No pending shutdown"
`shutdown /a` only works within the 60-second countdown window triggered by `/shutdown` or `/reboot`.

### Python `KeyError: 'TELEGRAM_BOT_TOKEN'`
The `.env` file is missing or in the wrong directory. Run `setup.ps1` again or create `.env` manually using `.env.example` as a template.
