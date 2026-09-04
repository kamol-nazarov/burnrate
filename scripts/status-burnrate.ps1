[CmdletBinding()]
param(
    [string]$TaskName = 'BURNRATE Dashboard'
)

# Status of the public BURNRATE dashboard on 127.0.0.1:17331.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BurnrateRuntime.ps1')
Get-BurnrateStatus -TaskName $TaskName | Format-List
