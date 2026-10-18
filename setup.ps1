#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
$ProjectDir  = "C:\Projects\QRemote"
$ServerScript = Join-Path $ProjectDir "server.py"
$EnvFile     = Join-Path $ProjectDir ".env"
$LogFile     = Join-Path $ProjectDir "QRemote.log"

function Write-Step([int]$n, [string]$msg) {
    Write-Host "`n[$n/7] $msg" -ForegroundColor Yellow
}

Write-Host "`n=== QRemote - Setup ===" -ForegroundColor Cyan

# -- 1. Python requirements ------------------------------------------------
Write-Step 1 "Installing Python requirements"
pip install -r "$ProjectDir\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install failed - is Python in PATH?" }
Write-Host "  requirements installed." -ForegroundColor Green

# -- 2. Scheduled task: RemoteShutdown --------------------------------------
# NOTE: these tasks run as the *interactive user*, NOT SYSTEM. The bot process
# (QRemoteBotStart) runs non-elevated as this same user, and a standard-user
# process cannot trigger a SYSTEM-owned task ("Access is denied"). Running the
# task as the user - who already holds SeShutdownPrivilege - lets the bot fire
# it via `schtasks /run` while still shutting the machine down reliably.
Write-Step 2 "Creating scheduled task: RemoteShutdown"
$currentUser = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

Register-ScheduledTask `
    -TaskName  "RemoteShutdown" `
    -Action    (New-ScheduledTaskAction -Execute "shutdown.exe" -Argument "/s /t 60") `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null
Write-Host "  RemoteShutdown task created (runs as $currentUser)." -ForegroundColor Green

# -- 3. Scheduled task: RemoteReboot ----------------------------------------
Write-Step 3 "Creating scheduled task: RemoteReboot"
Register-ScheduledTask `
    -TaskName  "RemoteReboot" `
    -Action    (New-ScheduledTaskAction -Execute "shutdown.exe" -Argument "/r /t 60") `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null
Write-Host "  RemoteReboot task created (runs as $currentUser)." -ForegroundColor Green

# -- 4. .env credentials -----------------------------------------------------
Write-Step 4 "Configuring environment (.env)"
if (Test-Path $EnvFile) {
    Write-Host "  .env already exists - skipping." -ForegroundColor Gray
} else {
    $token  = Read-Host "  Telegram Bot Token (from @BotFather)"
    $userId = Read-Host "  Your Telegram User ID  (from @userinfobot)"
    @"
TELEGRAM_BOT_TOKEN=$token
TELEGRAM_USER_ID=$userId
"@ | Set-Content $EnvFile -Encoding UTF8
    Write-Host "  .env written." -ForegroundColor Green
}

# -- 5. Log file --------------------------------------------------------------
Write-Step 5 "Initialising log file"
if (-not (Test-Path $LogFile)) {
    New-Item -ItemType File -Path $LogFile | Out-Null
    Write-Host "  Created $LogFile" -ForegroundColor Green
} else {
    Write-Host "  Log file already exists." -ForegroundColor Gray
}

# -- 6. Scheduled task: auto-start bot at logon -----------------------------
Write-Step 6 "Creating scheduled task: QRemoteBotStart"
$serverVbs    = Join-Path $ProjectDir "server_hidden.vbs"

$currentUser  = "$env:USERDOMAIN\$env:USERNAME"
$botPrincipal = New-ScheduledTaskPrincipal -UserId $currentUser -RunLevel Limited
$botTrigger   = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$botAction    = New-ScheduledTaskAction -Execute "wscript.exe" `
                    -Argument "`"$serverVbs`"" `
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

# -- 7. Scheduled task: CPU temperature helper --------------------------------
# The PawnIO sensor driver only accepts admin callers, so a small helper runs as SYSTEM
# and writes the temp to %ProgramData%\QRemote\cpu_temp.json for the (non-admin) bot.
# It runs from Program Files so a non-admin process can't swap the script it executes.
# Unlike steps 2-3, SYSTEM is fine here: it starts at boot and the bot never triggers it.
Write-Step 7 "Creating scheduled task: QRemoteCpuTemp"
$pawnDll = "$env:ProgramFiles\PawnIO\PawnIOLib.dll"
# PawnIO.Modules 0.2.11 module + SHA256 per CPU vendor
$modules = @{
    AuthenticAMD = @("AMDFamily17.bin", "DAE74615761B78BDF064DFB3E136252DDCC6FC727D88F14738D0E5800D427A91")
    GenuineIntel = @("IntelMSR.bin",    "D6ED85D65AB17A22F813EF98207D6D537155EE2DED5976A21CB48413C9B92E5F")
}
$vendor  = (Get-CimInstance Win32_Processor | Select-Object -First 1).Manufacturer
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if ($modules.ContainsKey($vendor) -and -not (Test-Path $pawnDll)) {
    Write-Host "  Installing PawnIO sensor driver..."
    winget install --id namazso.PawnIO --exact --accept-package-agreements --accept-source-agreements --disable-interactivity
}
if (-not $modules.ContainsKey($vendor)) {
    Write-Host "  Unsupported CPU vendor '$vendor' - skipping (AMD Ryzen and Intel only)." -ForegroundColor Gray
} elseif (-not (Test-Path $pawnDll)) {
    Write-Host "  PawnIO install failed - skipping. Try: winget install namazso.PawnIO" -ForegroundColor Yellow
} elseif (-not $pythonw -or $pythonw -like "*\WindowsApps\*") {
    # The Microsoft Store python alias can't run as SYSTEM
    Write-Host "  pythonw.exe not found (or only the Microsoft Store alias) - skipping. Install Python from python.org for all users." -ForegroundColor Yellow
} else {
    if ($pythonw -notlike "$env:ProgramFiles\*") {
        Write-Host "  WARNING: $pythonw is user-writable; it will run as SYSTEM. An all-users Python install is safer." -ForegroundColor Yellow
    }
    $helperDir = Join-Path $env:ProgramFiles "QRemote"
    New-Item -ItemType Directory -Force $helperDir | Out-Null
    Copy-Item (Join-Path $ProjectDir "cpu_temp_helper.py") $helperDir -Force

    $modName, $modHash = $modules[$vendor]
    $modPath = Join-Path $helperDir $modName
    if (-not (Test-Path $modPath) -or (Get-FileHash $modPath).Hash -ne $modHash) {
        $zip = Join-Path $env:TEMP "pawnio_modules.zip"
        Invoke-WebRequest "https://github.com/namazso/PawnIO.Modules/releases/download/0.2.11/release_0_2_11.zip" -OutFile $zip -UseBasicParsing
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $z = [IO.Compression.ZipFile]::OpenRead($zip)
        try { [IO.Compression.ZipFileExtensions]::ExtractToFile($z.GetEntry($modName), $modPath, $true) } finally { $z.Dispose() }
        Remove-Item $zip
        if ((Get-FileHash $modPath).Hash -ne $modHash) { Remove-Item $modPath; throw "$modName hash mismatch" }
    }

    Register-ScheduledTask `
        -TaskName  "QRemoteCpuTemp" `
        -Action    (New-ScheduledTaskAction -Execute $pythonw -Argument "`"$helperDir\cpu_temp_helper.py`"") `
        -Trigger   (New-ScheduledTaskTrigger -AtStartup) `
        -Principal (New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest) `
        -Settings  (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)) `
        -Force | Out-Null
    Stop-ScheduledTask -TaskName "QRemoteCpuTemp" -ErrorAction SilentlyContinue  # pick up a re-copied script
    Start-ScheduledTask -TaskName "QRemoteCpuTemp"
    Write-Host "  QRemoteCpuTemp task created and started (runs as SYSTEM at startup)." -ForegroundColor Green
}

# -- Done ---------------------------------------------------------------------
Write-Host "`n=== Setup complete! ===" -ForegroundColor Cyan
Write-Host @"

Next steps
----------
1. Install Cloudflare Tunnel (skip if already installed):
     winget install Cloudflare.cloudflared

2. Start the bot - tunnel + webhook registration happen automatically:
     python "$ServerScript"

   On first message you will receive "Bot online. Tunnel: https://xxx.trycloudflare.com"
   in Telegram confirming the webhook is live.

3. From now on the bot starts automatically at Windows logon (QRemoteBotStart task).
   No manual tunnel or webhook steps needed.

4. To add more bot commands, create a Task Scheduler task whose name starts with "Remote"
   (e.g. RemoteWinver). The bot will discover it automatically via /tasks.

See SETUP.md for named-tunnel setup and troubleshooting.
"@
