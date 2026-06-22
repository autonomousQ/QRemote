#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
$ProjectDir  = "C:\Projects\QRemote"
$ServerScript = Join-Path $ProjectDir "server.py"
$EnvFile     = Join-Path $ProjectDir ".env"
$LogFile     = Join-Path $ProjectDir "QRemote.log"

function Write-Step([int]$n, [string]$msg) {
    Write-Host "`n[$n/6] $msg" -ForegroundColor Yellow
}

Write-Host "`n=== QRemote — Setup ===" -ForegroundColor Cyan

# ── 1. Python requirements ────────────────────────────────────────────────────
Write-Step 1 "Installing Python requirements"
pip install -r "$ProjectDir\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install failed — is Python in PATH?" }
Write-Host "  requirements installed." -ForegroundColor Green

# ── 2. Scheduled task: RemoteShutdown ────────────────────────────────────────
Write-Step 2 "Creating scheduled task: RemoteShutdown"
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

Register-ScheduledTask `
    -TaskName  "RemoteShutdown" `
    -Action    (New-ScheduledTaskAction -Execute "shutdown.exe" -Argument "/s /t 60") `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null
Write-Host "  RemoteShutdown task created (runs as SYSTEM)." -ForegroundColor Green

# ── 3. Scheduled task: RemoteReboot ──────────────────────────────────────────
Write-Step 3 "Creating scheduled task: RemoteReboot"
Register-ScheduledTask `
    -TaskName  "RemoteReboot" `
    -Action    (New-ScheduledTaskAction -Execute "shutdown.exe" -Argument "/r /t 60") `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null
Write-Host "  RemoteReboot task created (runs as SYSTEM)." -ForegroundColor Green

# ── 4. .env credentials ───────────────────────────────────────────────────────
Write-Step 4 "Configuring environment (.env)"
if (Test-Path $EnvFile) {
    Write-Host "  .env already exists — skipping." -ForegroundColor Gray
} else {
    $token  = Read-Host "  Telegram Bot Token (from @BotFather)"
    $userId = Read-Host "  Your Telegram User ID  (from @userinfobot)"
    @"
TELEGRAM_BOT_TOKEN=$token
TELEGRAM_USER_ID=$userId
"@ | Set-Content $EnvFile -Encoding UTF8
    Write-Host "  .env written." -ForegroundColor Green
}

# ── 5. Log file ───────────────────────────────────────────────────────────────
Write-Step 5 "Initialising log file"
if (-not (Test-Path $LogFile)) {
    New-Item -ItemType File -Path $LogFile | Out-Null
    Write-Host "  Created $LogFile" -ForegroundColor Green
} else {
    Write-Host "  Log file already exists." -ForegroundColor Gray
}

# ── 6. Scheduled task: auto-start bot at logon ───────────────────────────────
Write-Step 6 "Creating scheduled task: QRemoteBotStart"
$serverCmd    = Join-Path $ProjectDir "server.cmd"

$currentUser  = "$env:USERDOMAIN\$env:USERNAME"
$botPrincipal = New-ScheduledTaskPrincipal -UserId $currentUser -RunLevel Limited
$botTrigger   = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$botAction    = New-ScheduledTaskAction -Execute "cmd.exe" `
                    -Argument "/c `"$serverCmd`"" `
                    -WorkingDirectory $ProjectDir
$botSettings  = New-ScheduledTaskSettingsSet `
                    -ExecutionTimeLimit ([TimeSpan]::Zero) `
                    -RestartCount 3 `
                    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName  "QRemoteBotStart" `
    -Action    $botAction `
    -Trigger   $botTrigger `
    -Principal $botPrincipal `
    -Settings  $botSettings `
    -Force | Out-Null
Write-Host "  QRemoteBotStart task created (runs at logon, restarts on failure)." -ForegroundColor Green

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host "`n=== Setup complete! ===" -ForegroundColor Cyan
Write-Host @"

Next steps
----------
1. Install Cloudflare Tunnel (skip if already installed):
     winget install Cloudflare.cloudflared

2. Start the bot — tunnel + webhook registration happen automatically:
     python "$ServerScript"

   On first message you will receive "Bot online. Tunnel: https://xxx.trycloudflare.com"
   in Telegram confirming the webhook is live.

3. From now on the bot starts automatically at Windows logon (QRemoteBotStart task).
   No manual tunnel or webhook steps needed.

4. To add more bot commands, create a Task Scheduler task whose name starts with "Remote"
   (e.g. RemoteWinver). The bot will discover it automatically via /tasks.

See SETUP.md for named-tunnel setup and troubleshooting.
"@
