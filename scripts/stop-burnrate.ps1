[CmdletBinding()]
param(
    [switch]$DryRun
)

# Stop only the public dashboard identified by PID file / command line
# containing spend_app.api or burnrate serve AND listening on 17331.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BurnrateRuntime.ps1')
Stop-BurnrateDashboard -DryRun:$DryRun
