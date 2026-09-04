# Install BURNRATE Python deps for CI. No provider keys. Repo-local only.
$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$installed = $false
if (Test-Path -LiteralPath "pyproject.toml") {
    python -m pip install -e ".[dev]"
    if ($LASTEXITCODE -eq 0) {
        $installed = $true
    }
    else {
        Write-Host "pip install -e .[dev] failed; trying -e . without extras"
        python -m pip install -e .
        if ($LASTEXITCODE -eq 0) { $installed = $true }
    }
}
if (-not $installed) {
    if (-not (Test-Path -LiteralPath "requirements-spend.txt")) {
        Write-Error "No pyproject.toml extra and no requirements-spend.txt"
        exit 1
    }
    python -m pip install -r requirements-spend.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

python -m pip install pytest ruff pip-audit
exit $LASTEXITCODE
