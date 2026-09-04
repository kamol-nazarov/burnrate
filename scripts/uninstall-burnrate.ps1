[CmdletBinding()]
param(
    [string]$TaskName = 'BURNRATE Dashboard',
    [switch]$PurgeData,
    [switch]$DryRun
)

# Unregister "BURNRATE Dashboard". Stop the process identified by the PID file
# or a command line containing spend_app.api or burnrate serve AND listening
# on 17331. Does not delete the database unless -PurgeData.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BurnrateRuntime.ps1')
Uninstall-BurnrateDashboard -TaskName $TaskName -PurgeData:$PurgeData -DryRun:$DryRun
