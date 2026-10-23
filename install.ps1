# Installs Xbox Wallpaper Changer for the current user (no admin needed).
#   - copies the app to %LOCALAPPDATA%\XboxWallpaper
#   - creates a Python virtual environment with its dependencies
#   - downloads the wallpapers and installs the "Xbox Wallpapers" Windows theme
#   - registers two scheduled tasks: weekly xbox.com sync, lock screen helper at logon
$ErrorActionPreference = 'Stop'

$AppDir = Join-Path $env:LOCALAPPDATA 'XboxWallpaper'
$Venv   = Join-Path $AppDir 'venv'
$Src    = $PSScriptRoot

# Terminals inside packaged apps (e.g. the Claude desktop app) get %LOCALAPPDATA% redirected to
# a private Packages\...\LocalCache folder that Task Scheduler and Windows themes can't see.
# Detect that and re-run this script through a one-time scheduled task, outside the package.
function Test-AppDataRedirected {
    $name = "xwp-probe-$([guid]::NewGuid()).tmp"
    $probe = Join-Path $env:LOCALAPPDATA $name
    Set-Content $probe ''
    $hit = Get-ChildItem "$env:LOCALAPPDATA\Packages\*\LocalCache\Local\$name" -ErrorAction SilentlyContinue
    Remove-Item $probe -ErrorAction SilentlyContinue
    return [bool]$hit
}
if (Test-AppDataRedirected) {
    Write-Host 'This terminal runs inside a packaged app; running the installer through Task Scheduler instead...'
    $logFile = Join-Path $env:TEMP 'xwp-install.log'
    Remove-Item $logFile -ErrorAction SilentlyContinue
    $taskName = 'XboxWallpaper - Install'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"& '$PSCommandPath' *> '$logFile'`""
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -TaskPath '\XboxWallpaper\' -Action $action -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskPath '\XboxWallpaper\' -TaskName $taskName
    $shown = 0
    do {
        Start-Sleep 2
        $lines = @(Get-Content $logFile -ErrorAction SilentlyContinue)
        $lines | Select-Object -Skip $shown | Write-Host
        $shown = $lines.Count
    } while ((Get-ScheduledTask -TaskPath '\XboxWallpaper\' -TaskName $taskName).State -eq 'Running')
    Get-Content $logFile -ErrorAction SilentlyContinue | Select-Object -Skip $shown | Write-Host
    Unregister-ScheduledTask -TaskPath '\XboxWallpaper\' -TaskName $taskName -Confirm:$false
    return
}

# Reuse the state of an earlier install that ended up in a redirected folder (see above),
# so the wallpapers already in Pictures aren't downloaded again, then remove that copy.
$stale = Get-ChildItem "$env:LOCALAPPDATA\Packages\*\LocalCache\Local\XboxWallpaper" -Directory -ErrorAction SilentlyContinue
foreach ($dir in $stale) {
    New-Item -ItemType Directory -Force $AppDir | Out-Null
    foreach ($f in 'config.json', 'state.json') {
        if ((Test-Path "$($dir.FullName)\$f") -and -not (Test-Path "$AppDir\$f")) { Copy-Item "$($dir.FullName)\$f" $AppDir }
    }
    Remove-Item -Recurse -Force $dir.FullName -ErrorAction SilentlyContinue
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'Python 3.10+ is required. Install it from https://www.python.org/downloads/ and re-run.' }

Write-Host "Installing to $AppDir"
New-Item -ItemType Directory -Force $AppDir | Out-Null
Copy-Item (Join-Path $Src 'xwp.py'), (Join-Path $Src 'requirements.txt'), (Join-Path $Src 'uninstall.ps1') $AppDir -Force

if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    & $python.Source -m venv $Venv
}
$py  = Join-Path $Venv 'Scripts\python.exe'
$pyw = Join-Path $Venv 'Scripts\pythonw.exe'
& $py -m pip install --disable-pip-version-check -q --upgrade pip
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $AppDir 'requirements.txt')
if ($LASTEXITCODE) { throw 'pip install failed' }

# Small launcher so you can run commands from anywhere: xwp sync / xwp next / xwp status
$cmd = Join-Path $AppDir 'xwp.cmd'
Set-Content $cmd "@`"$py`" `"$AppDir\xwp.py`" %*" -Encoding ASCII
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $AppDir) {
    [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ";$AppDir"), 'User')
}

# ---- first run -----------------------------------------------------------------
Write-Host ''
Write-Host 'First sync: downloading every wallpaper offered in the configured resolution (default 1920x1080).'
Write-Host 'This takes ~10-15 minutes and ~800 MB the first time...'
& $py (Join-Path $AppDir 'xwp.py') -v sync
if ($LASTEXITCODE) { throw 'sync failed - see the log above' }
& $py (Join-Path $AppDir 'xwp.py') -v apply
if ($LASTEXITCODE) { throw 'applying the theme failed - see the log above' }

# ---- scheduled tasks ----------------------------------------------------------
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

$syncSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
$sync   = New-ScheduledTaskAction -Execute $pyw -Argument "`"$AppDir\xwp.py`" sync" -WorkingDirectory $AppDir
$weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '12:00'
Register-ScheduledTask -TaskName 'XboxWallpaper - Sync' -TaskPath '\XboxWallpaper\' -Force `
    -Action $sync -Trigger $weekly -Principal $principal -Settings $syncSettings `
    -Description 'Weekly: downloads new wallpapers from xbox.com/wallpapers in the configured resolution.' | Out-Null

# Long-running and idle while waiting; restarted if it ever crashes.
$watchSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                 -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
                 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$watch = New-ScheduledTaskAction -Execute $pyw -Argument "`"$AppDir\xwp.py`" watch" -WorkingDirectory $AppDir
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
Register-ScheduledTask -TaskName 'XboxWallpaper - Watch' -TaskPath '\XboxWallpaper\' -Force `
    -Action $watch -Trigger $logon -Principal $principal -Settings $watchSettings `
    -Description 'Keeps the lock screen in sync with the Xbox Wallpapers theme and applies config changes.' | Out-Null
Start-ScheduledTask -TaskPath '\XboxWallpaper\' -TaskName 'XboxWallpaper - Watch'

Write-Host 'Scheduled tasks registered (weekly sync on Sundays 12:00 or next time the PC is on; lock screen helper at logon).'
Write-Host ''
Write-Host "Done. Theme 'Xbox Wallpapers' is active: Settings > Personalization > Themes."
Write-Host "Wallpapers: $([Environment]::GetFolderPath('MyPictures'))\Xbox Wallpapers"
Write-Host "Settings: $AppDir\config.json   Log: $AppDir\xwp.log"
