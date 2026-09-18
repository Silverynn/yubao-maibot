[CmdletBinding()]
param(
    [int]$CheckIntervalSeconds = 30,
    [int]$RestartCooldownSeconds = 60
)

$ErrorActionPreference = 'Stop'
$workspacePath = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $workspacePath '.runtime'
$maiBotPath = Join-Path $runtimePath 'MaiBot'
$venvPath = Join-Path $maiBotPath '.venv'
$venvPythonPath = Join-Path $venvPath 'Scripts\python.exe'
$patchPath = Join-Path $PSScriptRoot 'apply_response_guarantees.py'
$napCatPath = 'C:\Program Files\NapCatQQ Desktop\NapCatQQ-Desktop.exe'
$logPath = Join-Path $runtimePath 'watchdog.log'
$maiBotPidPath = Join-Path $runtimePath 'maibot.pid'

New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null

function Write-WatchdogLog {
    param([string]$Message)

    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Test-LocalPort {
    param([int]$Port)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connectTask = $client.ConnectAsync('127.0.0.1', $Port)
        return $connectTask.Wait(1000) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Start-NapCat {
    if (-not (Test-Path -LiteralPath $napCatPath -PathType Leaf)) {
        throw "NapCatQQ Desktop executable was not found: $napCatPath"
    }

    Start-Process -FilePath $napCatPath -WindowStyle Minimized
    Write-WatchdogLog 'NapCat was not listening on port 3001; launched NapCatQQ Desktop.'
}

function Test-MaiBotRunning {
    if (Test-LocalPort -Port 8001) {
        return $true
    }
    if (-not (Test-Path -LiteralPath $maiBotPidPath -PathType Leaf)) {
        return $false
    }

    $savedPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $maiBotPidPath -Raw).Trim(), [ref]$savedPid)) {
        return $false
    }

    return $null -ne (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)
}

function Start-MaiBot {
    if (-not (Test-Path -LiteralPath $venvPythonPath -PathType Leaf)) {
        throw "MaiBot Python executable was not found: $venvPythonPath"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $maiBotPath 'bot.py') -PathType Leaf)) {
        throw "MaiBot entry point was not found in: $maiBotPath"
    }

    # Do not pass launcher overrides inherited from a parent shell. The venv
    # interpreter resolves its own base Python correctly once these are absent.
    foreach ($variableName in @('__PYVENV_LAUNCHER__', 'PYTHONHOME', 'PYTHONEXECUTABLE')) {
        Remove-Item -LiteralPath "Env:$variableName" -ErrorAction SilentlyContinue
    }
    $pythonPath = $venvPythonPath
    # MaiBot's startup notice contains Unicode symbols. Force UTF-8 for hidden
    # watchdog launches so Windows' legacy GBK console encoding cannot crash it.
    $previousPythonUtf8 = $env:PYTHONUTF8
    $env:PYTHONUTF8 = '1'

    if (Test-Path -LiteralPath $patchPath -PathType Leaf) {
        $patchOutputPath = Join-Path $runtimePath 'watchdog-patch.stdout.log'
        $patchErrorPath = Join-Path $runtimePath 'watchdog-patch.stderr.log'
        $patchProcess = Start-Process `
            -FilePath $pythonPath `
            -ArgumentList @('-B', $patchPath) `
            -WorkingDirectory $workspacePath `
            -WindowStyle Hidden `
            -RedirectStandardOutput $patchOutputPath `
            -RedirectStandardError $patchErrorPath `
            -Wait `
            -PassThru
        if ($patchProcess.ExitCode -ne 0) {
            $patchError = (Get-Content -LiteralPath $patchErrorPath -Raw -ErrorAction SilentlyContinue).Trim()
            throw "Response guarantee patch failed with exit code $($patchProcess.ExitCode): $patchError"
        }
    }

    $maiBotProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @('-B', 'bot.py') `
        -WorkingDirectory $maiBotPath `
        -WindowStyle Hidden `
        -PassThru
    if ($null -eq $previousPythonUtf8) {
        Remove-Item -LiteralPath 'Env:PYTHONUTF8' -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONUTF8 = $previousPythonUtf8
    }
    Set-Content -LiteralPath $maiBotPidPath -Value $maiBotProcess.Id -Encoding ASCII
    Write-WatchdogLog 'MaiBot was not listening on port 8001; launched MaiBot.'
}

$createdNew = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\DaFeiYuBotWatchdog', [ref]$createdNew)
if (-not $createdNew) {
    Write-WatchdogLog 'Another watchdog instance is already running; exiting.'
    $mutex.Dispose()
    exit 0
}

$lastNapCatAttempt = [datetime]::MinValue
$lastMaiBotAttempt = [datetime]::MinValue
Write-WatchdogLog 'Watchdog started.'

try {
    while ($true) {
        $now = Get-Date
        $napCatReady = Test-LocalPort -Port 3001

        if (-not $napCatReady -and ($now - $lastNapCatAttempt).TotalSeconds -ge $RestartCooldownSeconds) {
            try {
                Start-NapCat
            }
            catch {
                Write-WatchdogLog "NapCat launch failed: $($_.Exception.Message)"
            }
            $lastNapCatAttempt = $now
        }

        if ($napCatReady) {
            $maiBotReady = Test-MaiBotRunning
            if (-not $maiBotReady -and ($now - $lastMaiBotAttempt).TotalSeconds -ge $RestartCooldownSeconds) {
                try {
                    Start-MaiBot
                }
                catch {
                    Write-WatchdogLog "MaiBot launch failed: $($_.Exception.Message)"
                }
                $lastMaiBotAttempt = $now
            }
        }

        Start-Sleep -Seconds $CheckIntervalSeconds
    }
}
finally {
    Write-WatchdogLog 'Watchdog stopped.'
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
