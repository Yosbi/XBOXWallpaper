# Removes the scheduled tasks, the theme and the app folder. Downloaded wallpapers are kept.
$ErrorActionPreference = 'Continue'
$AppDir = Join-Path $env:LOCALAPPDATA 'XboxWallpaper'

# Terminals inside packaged apps get %LOCALAPPDATA% redirected (see install.ps1), so run
# through a one-time scheduled task to remove the real files.
$name = "xwp-probe-$([guid]::NewGuid()).tmp"
Set-Content (Join-Path $env:LOCALAPPDATA $name) ''
$redirected = [bool](Get-ChildItem "$env:LOCALAPPDATA\Packages\*\LocalCache\Local\$name" -ErrorAction SilentlyContinue)
Remove-Item (Join-Path $env:LOCALAPPDATA $name) -ErrorAction SilentlyContinue
if ($redirected) {
    $copy = 'C:\Users\Public\xwp-uninstall.ps1'  # outside AppData, so the task sees it
    Copy-Item $PSCommandPath $copy -Force
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$copy`""
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'XboxWallpaper - Uninstall' -Action $action -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName 'XboxWallpaper - Uninstall'
    do { Start-Sleep 2 } while ((Get-ScheduledTask -TaskName 'XboxWallpaper - Uninstall').State -eq 'Running')
    Unregister-ScheduledTask -TaskName 'XboxWallpaper - Uninstall' -Confirm:$false
    Remove-Item $copy -ErrorAction SilentlyContinue
    Get-ChildItem "$env:LOCALAPPDATA\Packages\*\LocalCache\Local\XboxWallpaper" -Directory -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Uninstalled. Your wallpapers in '$([Environment]::GetFolderPath('MyPictures'))\Xbox Wallpapers' were kept."
    return
}

Get-ScheduledTask -TaskPath '\XboxWallpaper\' -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-ScheduledTask -TaskPath $_.TaskPath -TaskName $_.TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskPath $_.TaskPath -TaskName $_.TaskName -Confirm:$false
}
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*$AppDir*xwp.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Remove-Item (Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\Themes\Xbox Wallpapers.theme') -ErrorAction SilentlyContinue

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
[Environment]::SetEnvironmentVariable('Path', (($userPath -split ';') | Where-Object { $_ -and $_ -ne $AppDir }) -join ';', 'User')

Set-Location $env:TEMP
Remove-Item -Recurse -Force $AppDir -ErrorAction SilentlyContinue
Write-Host "Uninstalled. Your wallpapers in '$([Environment]::GetFolderPath('MyPictures'))\Xbox Wallpapers' were kept."
Write-Host 'Pick another theme or background in Settings > Personalization if the Xbox slideshow is still showing.'
