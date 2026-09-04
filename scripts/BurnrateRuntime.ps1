# Shared helpers for the public BURNRATE dashboard lifecycle scripts.
# Default bind is 127.0.0.1:17331. Self-heal is the scheduled task RestartCount
# (3 restarts, 1 minute interval). There is no separate supervisor script.

$ErrorActionPreference = 'Stop'

$script:BurnrateTaskName = 'BURNRATE Dashboard'
$script:BurnrateHost = '127.0.0.1'
$script:BurnratePort = 17331
$script:BurnrateMutexName = 'Local\BURNRATE-Dashboard'
$script:BurnrateAppSpec = 'spend_app.api:app'
$script:BurnrateLaunchModule = 'spend_app.service'
$script:BurnrateRestartCount = 3
$script:BurnrateRestartInterval = New-TimeSpan -Minutes 1

function Get-BurnrateDataRoot {
    if (-not [string]::IsNullOrWhiteSpace($env:BURNRATE_DATA_ROOT)) {
        return $env:BURNRATE_DATA_ROOT
    }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA is not set'
    }
    Join-Path $env:LOCALAPPDATA 'BURNRATE'
}

function Get-BurnrateRunDir {
    Join-Path (Get-BurnrateDataRoot) 'run'
}

function Get-BurnrateLogDir {
    Join-Path (Get-BurnrateDataRoot) 'logs'
}

function Get-BurnratePidFile {
    Join-Path (Get-BurnrateRunDir) 'burnrate.pid'
}

function Get-BurnrateDbPath {
    Join-Path (Get-BurnrateDataRoot) 'spend.db'
}

function Get-BurnrateHealthUri {
    "http://$($script:BurnrateHost):$($script:BurnratePort)/healthz"
}

function Initialize-BurnrateDirectories {
    [void](New-Item -ItemType Directory -Force -Path (Get-BurnrateRunDir))
    [void](New-Item -ItemType Directory -Force -Path (Get-BurnrateLogDir))
}

function Get-BurnrateRepoRoot {
    Split-Path -Parent $PSScriptRoot
}

function Get-BurnrateWorkingDirectory {
    $repoRoot = Get-BurnrateRepoRoot
    if (Test-Path -LiteralPath (Join-Path $repoRoot 'spend_app')) {
        return $repoRoot
    }
    return Get-BurnrateDataRoot
}

function ConvertTo-BurnratePythonw {
    param([Parameter(Mandatory = $true)][string]$Executable)
    $leaf = Split-Path -Leaf $Executable
    $dir = Split-Path -Parent $Executable
    if ($leaf -ieq 'pythonw.exe') {
        return $Executable
    }
    $pythonw = Join-Path $dir 'pythonw.exe'
    if (Test-Path -LiteralPath $pythonw) {
        return $pythonw
    }
    return $Executable
}

function Test-BurnratePython312 {
    param([Parameter(Mandatory = $true)][string]$Executable)
    $probe = $Executable
    $leaf = Split-Path -Leaf $Executable
    if ($leaf -ieq 'pythonw.exe') {
        $sibling = Join-Path (Split-Path -Parent $Executable) 'python.exe'
        if (Test-Path -LiteralPath $sibling) {
            $probe = $sibling
        }
    }
    try {
        $version = (& $probe -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")').Trim()
    } catch {
        return $false
    }
    return $version -eq '3.12'
}

function Resolve-BurnratePythonw {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:BURNRATE_PYTHON)) {
        $candidates += $env:BURNRATE_PYTHON
    }
    $candidates += Join-Path (Join-Path (Get-BurnrateDataRoot) 'venv') 'Scripts\pythonw.exe'
    $candidates += Join-Path (Join-Path (Get-BurnrateRepoRoot) '.venv') 'Scripts\pythonw.exe'

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $fromPy = (& py -3.12 -c 'import sys; print(sys.executable)').Trim()
            if ($fromPy) {
                $candidates += $fromPy
            }
        } catch {
        }
    }

    foreach ($name in @('pythonw', 'python')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) {
            $candidates += $cmd.Source
        }
    }

    $observed = @()
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate) -or -not (Test-Path -LiteralPath $candidate)) {
            continue
        }
        $pythonw = ConvertTo-BurnratePythonw -Executable $candidate
        if (-not (Test-Path -LiteralPath $pythonw)) {
            $observed += "$candidate (no pythonw)"
            continue
        }
        if (Test-BurnratePython312 -Executable $pythonw) {
            return (Resolve-Path -LiteralPath $pythonw).Path
        }
        $observed += "$pythonw (not 3.12)"
    }
    $detail = if ($observed.Count -gt 0) { $observed -join '; ' } else { 'no candidates' }
    throw "BURNRATE requires CPython 3.12 with pythonw.exe. Set BURNRATE_PYTHON. Checked: $detail"
}

function Get-BurnrateArgumentList {
    # Windowless autostart: pythonw -m spend_app (spend_app.service).
    # Foreground equivalent: burnrate serve
    # --app spend_app.api:app is the process marker uninstall matches.
    @(
        '-m', $script:BurnrateLaunchModule,
        '--host', $script:BurnrateHost,
        '--port', [string]$script:BurnratePort,
        '--app', $script:BurnrateAppSpec
    )
}

function ConvertTo-BurnrateArgumentString {
    param([Parameter(Mandatory = $true)][string[]]$Parts)
    ($Parts | ForEach-Object {
        if ($_ -match '[\s"]') {
            '"' + ($_ -replace '"', '\"') + '"'
        } else {
            $_
        }
    }) -join ' '
}

function Get-BurnrateLaunchSpec {
    param([switch]$AllowMissingPython)
    $arguments = @(Get-BurnrateArgumentList)
    $pythonw = $null
    try {
        $pythonw = Resolve-BurnratePythonw
    } catch {
        if (-not $AllowMissingPython) {
            throw
        }
        $pythonw = 'pythonw.exe'
        Write-Host "pythonw not resolved: $_"
    }
    [pscustomobject]@{
        FilePath         = $pythonw
        ArgumentList     = $arguments
        ArgumentString   = ConvertTo-BurnrateArgumentString -Parts $arguments
        WorkingDirectory = Get-BurnrateWorkingDirectory
        Host             = $script:BurnrateHost
        Port             = $script:BurnratePort
        MutexName        = $script:BurnrateMutexName
        PidFile          = Get-BurnratePidFile
        DataRoot         = Get-BurnrateDataRoot
    }
}

function Test-BurnrateCommandLine {
    param([string]$CommandLine)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    $markers = @(
        'spend_app.api',
        'burnrate serve',
        'spend_app.service',
        '-m spend_app'
    )
    foreach ($marker in $markers) {
        if ($CommandLine.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            return $true
        }
    }
    return $false
}

function Get-BurnrateListeners {
    try {
        $rows = @(Get-NetTCPConnection -LocalPort $script:BurnratePort -State Listen -ErrorAction Stop)
    } catch {
        return @()
    }
    $rows | Where-Object {
        $_.LocalAddress -eq '127.0.0.1' -or $_.LocalAddress -eq '::1'
    }
}

function Get-BurnrateProcessById {
    param([int]$ProcessId)
    Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Test-BurnrateOwnedProcess {
    param($Process)
    if (-not $Process) {
        return $false
    }
    return Test-BurnrateCommandLine -CommandLine $Process.CommandLine
}

function Read-BurnratePidFile {
    $pidFile = Get-BurnratePidFile
    if (-not (Test-Path -LiteralPath $pidFile)) {
        return $null
    }
    $raw = (Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    $raw = $raw.Trim()
    if ($raw -notmatch '^\d+$') {
        return $null
    }
    return [int]$raw
}

function Write-BurnratePidFile {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    Initialize-BurnrateDirectories
    Set-Content -LiteralPath (Get-BurnratePidFile) -Value ([string]$ProcessId) -Encoding ascii
}

function Remove-BurnratePidFile {
    $pidFile = Get-BurnratePidFile
    if (Test-Path -LiteralPath $pidFile) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

function Test-BurnrateHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri (Get-BurnrateHealthUri)
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    } catch {
        return $false
    }
}

function Wait-BurnrateHealth {
    param([int]$Seconds = 30)
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-BurnrateHealth) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for $(Get-BurnrateHealthUri)"
}

function Assert-BurnrateLoopbackBind {
    $listeners = @(Get-BurnrateListeners)
    if ($listeners.Count -eq 0) {
        throw "BURNRATE is not listening on $($script:BurnrateHost):$($script:BurnratePort)"
    }
    foreach ($listener in $listeners) {
        if ($listener.LocalAddress -ne '127.0.0.1' -and $listener.LocalAddress -ne '::1') {
            throw "BURNRATE did not bind exclusively to $($script:BurnrateHost):$($script:BurnratePort) (got $($listener.LocalAddress))"
        }
    }
}

function Get-CurrentWindowsIdentityName {
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

function Get-BurnrateScheduledTask {
    param([string]$TaskName = $script:BurnrateTaskName)
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Test-BurnrateTaskPayload {
    param($Task)
    if (-not $Task) {
        return $false
    }
    $action = @($Task.Actions) | Select-Object -First 1
    if (-not $action) {
        return $false
    }
    $execute = [string]$action.Execute
    $arguments = [string]$action.Arguments
    if ($execute.IndexOf('pythonw', [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $false
    }
    if ($arguments.IndexOf([string]$script:BurnratePort, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $false
    }
    if ($arguments.IndexOf($script:BurnrateHost, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $false
    }
    return Test-BurnrateCommandLine -CommandLine "$execute $arguments"
}

function Register-BurnrateDashboardTask {
    param(
        [string]$TaskName = $script:BurnrateTaskName,
        [Parameter(Mandatory = $true)]$Launch,
        [switch]$DryRun
    )

    if (-not $DryRun) {
        $existing = Get-BurnrateScheduledTask -TaskName $TaskName
        if ($existing -and -not (Test-BurnrateTaskPayload -Task $existing)) {
            throw "Scheduled task '$TaskName' exists but is not a BURNRATE dashboard task. Aborting."
        }
    }

    $identity = Get-CurrentWindowsIdentityName
    $action = New-ScheduledTaskAction -Execute $Launch.FilePath -Argument $Launch.ArgumentString -WorkingDirectory $Launch.WorkingDirectory
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -RestartCount $script:BurnrateRestartCount `
        -RestartInterval $script:BurnrateRestartInterval `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable `
        -Hidden
    $description = 'BURNRATE dashboard. Windowless pythonw bound to 127.0.0.1:17331 at sign-in. Self-heal is RestartCount 3 / RestartInterval 1 minute, not a separate supervisor script.'
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $description

    if ($DryRun) {
        Write-Host "DryRun: would register scheduled task '$TaskName' (AtLogOn, Limited, Hidden, RestartCount $($script:BurnrateRestartCount))"
        Write-Host "DryRun: action $($Launch.FilePath) $($Launch.ArgumentString)"
        return
    }

    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Write-Host "Registered scheduled task '$TaskName'"
}

function Unregister-BurnrateDashboardTask {
    param(
        [string]$TaskName = $script:BurnrateTaskName,
        [switch]$DryRun
    )
    $existing = Get-BurnrateScheduledTask -TaskName $TaskName
    if (-not $existing) {
        Write-Host "Scheduled task '$TaskName' is not registered."
        return
    }
    if ($DryRun) {
        Write-Host "DryRun: would unregister scheduled task '$TaskName'"
        return
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered scheduled task '$TaskName'"
}

function Start-BurnrateDashboard {
    param([switch]$DryRun)

    if (-not $DryRun) {
        Initialize-BurnrateDirectories
    }
    $launch = Get-BurnrateLaunchSpec -AllowMissingPython:$DryRun

    if ($DryRun) {
        Write-Host "DryRun: would start pythonw -m spend_app ($($launch.FilePath) $($launch.ArgumentString))"
        Write-Host "DryRun: working directory $($launch.WorkingDirectory)"
        Write-Host "DryRun: mutex $($script:BurnrateMutexName); pid file $($launch.PidFile)"
        return
    }

    $listeners = @(Get-BurnrateListeners)

    if ($listeners.Count -gt 0) {
        $foreign = $false
        foreach ($listener in $listeners) {
            $process = Get-BurnrateProcessById -ProcessId ([int]$listener.OwningProcess)
            if (Test-BurnrateOwnedProcess -Process $process) {
                Write-BurnratePidFile -ProcessId ([int]$listener.OwningProcess)
                Write-Host "BURNRATE dashboard already listening on $($script:BurnrateHost):$($script:BurnratePort) (PID $($listener.OwningProcess))"
                return
            }
            $foreign = $true
        }
        if ($foreign) {
            $owner = $listeners[0]
            throw "Port $($script:BurnratePort) is already owned by PID $($owner.OwningProcess). Not stealing it."
        }
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $launch.FilePath
    $psi.Arguments = $launch.ArgumentString
    $psi.WorkingDirectory = $launch.WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $started = [System.Diagnostics.Process]::Start($psi)
    if (-not $started) {
        throw "Failed to start $($launch.FilePath)"
    }
    Write-BurnratePidFile -ProcessId $started.Id
    try {
        Wait-BurnrateHealth
        Assert-BurnrateLoopbackBind
    } catch {
        throw
    }
    Write-Host "BURNRATE dashboard listening on $($script:BurnrateHost):$($script:BurnratePort) (PID $($started.Id))"
}

function Stop-BurnrateDashboard {
    param([switch]$DryRun)

    $targets = @()
    $pidValue = Read-BurnratePidFile
    if ($pidValue) {
        $fromFile = Get-BurnrateProcessById -ProcessId $pidValue
        if (Test-BurnrateOwnedProcess -Process $fromFile) {
            $targets += $pidValue
        } elseif ($fromFile -and -not $fromFile.CommandLine) {
            Write-Host "Refusing to stop PID $pidValue because the command line is unreadable."
        } elseif ($fromFile) {
            Write-Host "PID file $pidValue is not a BURNRATE dashboard process; leaving it running."
        }
    }

    foreach ($listener in @(Get-BurnrateListeners)) {
        $processId = [int]$listener.OwningProcess
        $process = Get-BurnrateProcessById -ProcessId $processId
        if ((Test-BurnrateOwnedProcess -Process $process) -and ($targets -notcontains $processId)) {
            $targets += $processId
        }
    }

    if ($targets.Count -eq 0) {
        if (-not $DryRun) {
            Remove-BurnratePidFile
        }
        Write-Host 'BURNRATE dashboard is not running.'
        return
    }

    foreach ($processId in $targets) {
        if ($DryRun) {
            Write-Host "DryRun: would stop BURNRATE dashboard PID $processId (port $($script:BurnratePort) only)"
            continue
        }
        Stop-Process -Id $processId -ErrorAction SilentlyContinue
        Write-Host "Stopped BURNRATE dashboard (PID $processId)"
    }
    if (-not $DryRun) {
        Remove-BurnratePidFile
    }
}

function Get-BurnrateStatus {
    param([string]$TaskName = $script:BurnrateTaskName)
    $task = Get-BurnrateScheduledTask -TaskName $TaskName
    $listeners = @(Get-BurnrateListeners)
    $listener = $listeners | Select-Object -First 1
    $pidValue = if ($listener) { [int]$listener.OwningProcess } else { Read-BurnratePidFile }
    $healthy = Test-BurnrateHealth
    [pscustomobject]@{
        task         = $TaskName
        registered   = [bool]$task
        hidden       = if ($task) { [bool]$task.Settings.Hidden } else { $null }
        restartCount = if ($task) { $task.Settings.RestartCount } else { $null }
        host         = $script:BurnrateHost
        port         = $script:BurnratePort
        listening    = [bool]$listener
        address      = if ($listener) { $listener.LocalAddress } else { $null }
        pid          = $pidValue
        health       = if ($healthy) { 'ok' } else { 'down' }
        pidFile      = Get-BurnratePidFile
        dataRoot     = Get-BurnrateDataRoot
        mutex        = $script:BurnrateMutexName
    }
}

function Install-BurnrateDashboard {
    param(
        [string]$TaskName = $script:BurnrateTaskName,
        [switch]$DryRun
    )
    if (-not $DryRun) {
        Initialize-BurnrateDirectories
    }
    $launch = Get-BurnrateLaunchSpec -AllowMissingPython:$DryRun
    Write-Host "pythonw: $($launch.FilePath)"
    Write-Host "bind: $($script:BurnrateHost):$($script:BurnratePort)"
    Write-Host "mutex: $($script:BurnrateMutexName)"
    Write-Host "pid file: $($launch.PidFile)"
    Write-Host "data root: $($launch.DataRoot)"
    Write-Host "self-heal: scheduled task RestartCount $($script:BurnrateRestartCount) / RestartInterval $($script:BurnrateRestartInterval)"
    Register-BurnrateDashboardTask -TaskName $TaskName -Launch $launch -DryRun:$DryRun
    if ($DryRun) {
        Start-BurnrateDashboard -DryRun
        return
    }
    if (Test-BurnrateHealth) {
        Write-Host "BURNRATE dashboard already healthy at $(Get-BurnrateHealthUri)"
        return
    }
    Start-BurnrateDashboard
}

function Uninstall-BurnrateDashboard {
    param(
        [string]$TaskName = $script:BurnrateTaskName,
        [switch]$PurgeData,
        [switch]$DryRun
    )
    Stop-BurnrateDashboard -DryRun:$DryRun
    Unregister-BurnrateDashboardTask -TaskName $TaskName -DryRun:$DryRun
    $dbPath = Get-BurnrateDbPath
    $dataRoot = Get-BurnrateDataRoot
    if ($PurgeData) {
        if ($DryRun) {
            Write-Host "DryRun: would purge data root $dataRoot"
            return
        }
        if (Test-Path -LiteralPath $dataRoot) {
            Remove-Item -LiteralPath $dataRoot -Recurse -Force
            Write-Host "Purged $dataRoot"
        }
        return
    }
    Write-Host "Database kept at $dbPath (pass -PurgeData to delete)."
}
