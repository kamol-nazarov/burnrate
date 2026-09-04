# Fail CI if provider credentials or live-db pointers leaked into the job env.
$ErrorActionPreference = "Stop"

$forbidden = @(
    "OPENAI_ADMIN_KEY",
    "ANTHROPIC_ADMIN_KEY",
    "CURSOR_API_KEY",
    "OPENROUTER_MANAGEMENT_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "SPEND_REAL_DB_COPY",
    "BURNRATE_CLAUDE_OAUTH_REFRESH"
)

$present = @()
foreach ($name in $forbidden) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if (-not [string]::IsNullOrEmpty($value)) {
        $present += $name
    }
}

if ($present.Count -gt 0) {
    Write-Error ("CI must not set provider credentials or live-db paths: " + ($present -join ", "))
    exit 1
}

Write-Host "Provider credential env vars are unset (synthetic fixtures only)."
exit 0
