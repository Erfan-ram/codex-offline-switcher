[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($env:TERM_PROGRAM -eq 'vscode') {
    throw 'Run this script from a standalone PowerShell window, outside VS Code.'
}

$sessionId = (Get-Process -Id $PID).SessionId
$appPattern = '^(?:ChatGPT|(?:OpenAI[- ]?)?Codex|Code|VSCode|Visual Studio Code)(?:$|[- .])'

function Get-AuthBlockingProcesses {
    Get-Process | Where-Object {
        $_.ProcessName -ne 'codex-windows-sandbox-service' -and
        $_.ProcessName -match $appPattern
    }
}

function Format-Processes($Processes) {
    ($Processes | ForEach-Object { "$($_.ProcessName) (PID $($_.Id), session $($_.SessionId))" }) -join ', '
}

$running = @(Get-AuthBlockingProcesses)
$localProcesses = @($running | Where-Object SessionId -eq $sessionId)
$requestedClose = $false

# Give visible apps time to save and shut down normally.
foreach ($process in $localProcesses) {
    try {
        if ($process.MainWindowHandle -ne [IntPtr]::Zero -and $process.CloseMainWindow()) {
            Write-Host "Requested a normal quit for $($process.ProcessName) (PID $($process.Id))."
            $requestedClose = $true
        }
    } catch {
        if (-not $process.HasExited) { Write-Warning "Could not request a normal quit for PID $($process.Id)." }
    }
}

if ($requestedClose) {
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 500
        $localProcesses = @(Get-AuthBlockingProcesses | Where-Object SessionId -eq $sessionId)
    } while ($localProcesses.Count -and [DateTime]::UtcNow -lt $deadline)
}

# Refresh the process list each pass to catch workers that briefly restart.
for ($attempt = 0; $attempt -lt 6; $attempt++) {
    $localProcesses = @(Get-AuthBlockingProcesses | Where-Object SessionId -eq $sessionId)
    foreach ($process in $localProcesses) {
        try {
            if (-not $process.HasExited) {
                Stop-Process -InputObject $process -ErrorAction Stop
                Write-Host "Stopped $($process.ProcessName) (PID $($process.Id))."
            }
        } catch {
            if (-not $process.HasExited) { Write-Warning "Could not stop $($process.ProcessName) (PID $($process.Id))." }
        }
    }

    Start-Sleep -Milliseconds 750
    $running = @(Get-AuthBlockingProcesses)
    if (-not @($running | Where-Object SessionId -eq $sessionId).Count) { break }
}

$running = @(Get-AuthBlockingProcesses)
if ($running.Count) {
    throw "Close the remaining processes before running the installer: $(Format-Processes $running)"
}

Write-Host 'Codex, ChatGPT Desktop, and VS Code are closed.'
