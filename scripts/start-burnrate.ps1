[CmdletBinding()]
param(
    [switch]$DryRun
)

# Start the windowless dashboard: pythonw -m spend_app on 127.0.0.1:17331.
# Equivalent foreground command: burnrate serve

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BurnrateRuntime.ps1')
Start-BurnrateDashboard -DryRun:$DryRun
