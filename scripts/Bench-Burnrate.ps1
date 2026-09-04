[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:17331',
    [int]$Requests = 20,
    [string]$Baseline = (Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\perf-baseline.json')
)

# Measures the BURNRATE API and assets. Read-only: summary, health, and static
# assets only. Never probes provider-limit collectors.
# A baseline JSON is optional; without it the script still prints p50/p95.

$ErrorActionPreference = 'Stop'
$limits = $null
if (Test-Path -LiteralPath $Baseline) {
    $limits = Get-Content -Raw -LiteralPath $Baseline | ConvertFrom-Json
}

function Measure-Endpoint([string]$Uri, [int]$Count) {
    $samples = @()
    for ($i = 0; $i -lt $Count; $i++) {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $null = Invoke-WebRequest -UseBasicParsing -Uri $Uri
        $sw.Stop()
        $samples += $sw.Elapsed.TotalMilliseconds
    }
    # The first request after a data change is the cold computation; the bench
    # measures the steady state every later poll pays.
    $warm = @($samples | Select-Object -Skip 1 | Sort-Object)
    [pscustomobject]@{
        cold = [math]::Round($samples[0], 1)
        p50  = [math]::Round($warm[[math]::Floor(($warm.Count - 1) * 0.50)], 1)
        p95  = [math]::Round($warm[[math]::Floor(($warm.Count - 1) * 0.95)], 1)
        max  = [math]::Round(($warm | Measure-Object -Maximum).Maximum, 1)
    }
}

function Measure-Transfer([string]$Uri) {
    $gzip = & curl.exe -s -o NUL -w '%{size_download}' -H 'Accept-Encoding: gzip' $Uri
    $raw = & curl.exe -s -o NUL -w '%{size_download}' -H 'Accept-Encoding: identity' $Uri
    [pscustomobject]@{ gzip = [int]$gzip; raw = [int]$raw }
}

$failures = @()
$rows = @()
$windows = if ($limits -and $limits.summary_p95_ms) {
    @($limits.summary_p95_ms.PSObject.Properties.Name)
} else {
    @('15m', '1h', '1d', '1w')
}
foreach ($window in $windows) {
    $result = Measure-Endpoint "$BaseUrl/api/spend/summary?window=$window&tool=all" $Requests
    $limit = if ($limits) { [double]$limits.summary_p95_ms.$window } else { $null }
    $rows += [pscustomobject]@{ endpoint = "summary $window"; cold_ms = $result.cold; p50_ms = $result.p50; p95_ms = $result.p95; max_ms = $result.max; limit_ms = $limit }
    if ($null -ne $limit -and $result.p95 -gt $limit) { $failures += "summary $window p95 $($result.p95) ms > $limit ms" }
}
$health = Measure-Endpoint "$BaseUrl/api/spend/health" $Requests
$healthLimit = if ($limits) { [double]$limits.health_p95_ms } else { $null }
$rows += [pscustomobject]@{ endpoint = 'health'; cold_ms = $health.cold; p50_ms = $health.p50; p95_ms = $health.p95; max_ms = $health.max; limit_ms = $healthLimit }
if ($null -ne $healthLimit -and $health.p95 -gt $healthLimit) { $failures += "health p95 $($health.p95) ms > $healthLimit ms" }
$rows | Format-Table -AutoSize | Out-String -Width 160

$html = Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/"
$jsPath = ([regex]::Match($html.Content, '/spend\.js\?v=[0-9a-f]+')).Value
$cssPath = ([regex]::Match($html.Content, '/spend\.css\?v=[0-9a-f]+')).Value
$assets = @(
    [pscustomobject]@{ asset = 'index.html'; sizes = (Measure-Transfer "$BaseUrl/"); limit = if ($limits) { [int]$limits.html_gzip_bytes_max } else { $null } },
    [pscustomobject]@{ asset = 'spend.js'; sizes = (Measure-Transfer "$BaseUrl$jsPath"); limit = if ($limits) { [int]$limits.js_gzip_bytes_max } else { $null } },
    [pscustomobject]@{ asset = 'spend.css'; sizes = (Measure-Transfer "$BaseUrl$cssPath"); limit = if ($limits) { [int]$limits.css_gzip_bytes_max } else { $null } },
    [pscustomobject]@{ asset = 'summary 15m'; sizes = (Measure-Transfer "$BaseUrl/api/spend/summary?window=15m&tool=all"); limit = if ($limits) { [int]$limits.summary_gzip_bytes_max } else { $null } }
)
$assets | ForEach-Object { [pscustomobject]@{ asset = $_.asset; gzip_bytes = $_.sizes.gzip; raw_bytes = $_.sizes.raw; limit_bytes = $_.limit } } | Format-Table -AutoSize | Out-String -Width 120
foreach ($asset in $assets) {
    if ($null -ne $asset.limit -and $asset.sizes.gzip -gt $asset.limit) { $failures += "$($asset.asset) gzip $($asset.sizes.gzip) B > $($asset.limit) B" }
}
$immutable = (Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl$jsPath").Headers['Cache-Control']
if ($immutable -notmatch 'immutable') { $failures += "hashed asset is not served immutable ($immutable)" }

if ($failures.Count -gt 0) {
    Write-Host "PERF REGRESSION: $($failures -join '; ')" -ForegroundColor Red
    exit 1
}
Write-Host 'Perf within baseline.' -ForegroundColor Green
