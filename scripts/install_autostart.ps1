[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$watchdogPath = Join-Path $PSScriptRoot 'ensure_bot_running.ps1'

if (-not (Test-Path -LiteralPath $watchdogPath -PathType Leaf)) {
    throw "Watchdog script was not found: $watchdogPath"
}

$legacyTaskName = 'DaFeiYu-Bot-Watchdog'
if (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false
}

$powerShellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$startupPath = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupPath 'DaFeiYu Bot Watchdog.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powerShellPath
$shortcut.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $watchdogPath
$shortcut.WorkingDirectory = Split-Path -Parent $PSScriptRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Keeps NapCatQQ Desktop and DaFeiYu MaiBot running after Windows logon.'
$shortcut.Save()

Start-Process `
    -FilePath $powerShellPath `
    -ArgumentList ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $watchdogPath) `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot) `
    -WindowStyle Hidden
Write-Output "Installed and started login shortcut: $shortcutPath"
