[CmdletBinding()]
param(
    [string]$TaskName = 'BURNRATE Dashboard',
    [switch]$DryRun
)

# Per-user scheduled task "BURNRATE Dashboard": AtLogOn, Limited, Hidden.
# Action starts pythonw -m spend_app (spend_app.service) bound to 127.0.0.1:17331.
# Foreground equivalent: burnrate serve
# Self-heal is RestartCount 3 / RestartInterval 1 minute, not a separate supervisor.
# Mutex Local\BURNRATE-Dashboard; PID under $env:LOCALAPPDATA\BURNRATE\run.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BurnrateRuntime.ps1')
Install-BurnrateDashboard -TaskName $TaskName -DryRun:$DryRun
