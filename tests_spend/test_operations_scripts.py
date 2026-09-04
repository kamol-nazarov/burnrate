from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from spend_app import service as burnrate_service

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

PUBLIC_SCRIPTS = (
    "install-burnrate.ps1",
    "uninstall-burnrate.ps1",
    "start-burnrate.ps1",
    "stop-burnrate.ps1",
    "status-burnrate.ps1",
    "BurnrateRuntime.ps1",
    "Bench-Burnrate.ps1",
)

PRIVATE_SCRIPTS = (
    "Start-AiTelemetry.ps1",
    "Stop-AiTelemetry.ps1",
    "Status-AiTelemetry.ps1",
    "Test-AiTelemetry.ps1",
    "Install-Binaries.ps1",
    "Enable-AiTelemetryAutostart.ps1",
    "Disable-AiTelemetryAutostart.ps1",
    "Watch-AiTelemetry.ps1",
)

FORBIDDEN_TOKENS = (
    "3060",
    "3050",
    "3000",
    "8888",
    "8889",
    "9090",
    "9470",
    "4317",
    "4318",
    "13133",
    "3443",
    "3445",
    "tailscale",
    "Tailscale",
    "tail982d0e",
    "mydesktop",
    ".ts.net",
    "AiTelemetry",
    "Watch-AiTelemetry",
    "Self-Healing Monitor",
    "VehicleDesk",
    "vehicledesk",
    "kamol",
    "ai-telemetry",
    "0.0.0.0",
    "C:\\Users\\",
    r"C:\Users",
)


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def _all_scripts_text() -> str:
    return "\n".join(_read(name) for name in PUBLIC_SCRIPTS)


def _powershell() -> str:
    for name in ("pwsh", "powershell"):
        path = shutil.which(name)
        if path:
            return path
    pytest.skip("PowerShell is not available")


def test_private_six_process_scripts_are_gone() -> None:
    for name in PRIVATE_SCRIPTS:
        assert not (SCRIPTS / name).exists(), name
    remaining = {path.name for path in SCRIPTS.glob("*")}
    for name in PRIVATE_SCRIPTS:
        assert name not in remaining


def test_public_lifecycle_scripts_exist() -> None:
    for name in PUBLIC_SCRIPTS:
        assert (SCRIPTS / name).is_file(), name


def test_public_scripts_bind_localhost_17331() -> None:
    corpus = _all_scripts_text()
    assert "127.0.0.1" in corpus
    assert "17331" in corpus
    assert "http://127.0.0.1:17331" in _read("Bench-Burnrate.ps1")
    install = _read("install-burnrate.ps1") + "\n" + _read("BurnrateRuntime.ps1")
    assert "127.0.0.1" in install
    assert "17331" in install
    start = _read("start-burnrate.ps1") + "\n" + _read("BurnrateRuntime.ps1")
    assert "127.0.0.1" in start
    assert "17331" in start


def test_public_scripts_use_pythonw_and_task_name() -> None:
    install = _read("install-burnrate.ps1") + "\n" + _read("BurnrateRuntime.ps1")
    assert "BURNRATE Dashboard" in install
    assert "pythonw" in install
    assert "-m spend_app" in install
    assert "burnrate serve" in install
    assert "AtLogOn" in install
    assert "Limited" in install
    assert "Hidden" in install
    assert r"Local\BURNRATE-Dashboard" in install
    assert "LOCALAPPDATA" in install
    assert "RestartCount" in install
    assert "spend_app.api" in install


def test_uninstall_identifies_public_process_and_keeps_db() -> None:
    uninstall = _read("uninstall-burnrate.ps1") + "\n" + _read("BurnrateRuntime.ps1")
    assert "BURNRATE Dashboard" in uninstall
    assert "17331" in uninstall
    assert "spend_app.api" in uninstall
    assert "burnrate serve" in uninstall
    assert "-PurgeData" in uninstall
    assert "Database kept" in uninstall
    assert "spend.db" in uninstall
    purge_index = uninstall.index("if ($PurgeData)")
    kept_index = uninstall.index("Database kept")
    assert kept_index != purge_index
    assert "Remove-Item -LiteralPath $dataRoot" in uninstall[purge_index:]
    assert "Remove-Item -LiteralPath $dataRoot" not in uninstall[:purge_index]


def test_public_scripts_have_no_private_stack_tokens() -> None:
    corpus = _all_scripts_text()
    for token in FORBIDDEN_TOKENS:
        assert token not in corpus, token
    service_text = (ROOT / "spend_app" / "service.py").read_text(encoding="utf-8")
    for token in FORBIDDEN_TOKENS:
        assert token not in service_text, token


def test_bench_defaults_to_public_port() -> None:
    script = _read("Bench-Burnrate.ps1")
    assert "$BaseUrl = 'http://127.0.0.1:17331'" in script
    assert "3060" not in script


def test_stop_only_targets_public_listener() -> None:
    runtime = _read("BurnrateRuntime.ps1")
    assert "LocalPort $script:BurnratePort" in runtime
    assert "Get-NetTCPConnection -LocalPort $script:BurnratePort" in runtime
    assert "spend_app.api" in runtime
    assert "burnrate serve" in runtime
    assert "17331" in runtime
    assert "Stop-Process" in runtime
    assert "3060" not in runtime


def test_powershell_scripts_parse() -> None:
    shell = _powershell()
    for name in PUBLIC_SCRIPTS:
        path = SCRIPTS / name
        escaped = str(path).replace("'", "''")
        command = (
            "$e = $null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{escaped}', [ref]$null, [ref]$e); "
            "if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        completed = subprocess.run(
            [shell, "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, f"{name}: {completed.stdout}{completed.stderr}"


def test_install_dry_run_does_not_register_live_task(tmp_path: Path) -> None:
    shell = _powershell()
    install = SCRIPTS / "install-burnrate.ps1"
    task_name = "BURNRATE-Lane-I3-DryRun"
    env = os.environ.copy()
    env["BURNRATE_DATA_ROOT"] = str(tmp_path / "data")
    try:
        completed = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-File",
                str(install),
                "-DryRun",
                "-TaskName",
                task_name,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        output = completed.stdout + completed.stderr
        assert completed.returncode == 0, output
        assert "DryRun" in output
        assert task_name in output
        assert "127.0.0.1" in output
        assert "17331" in output
        assert "would register" in output.lower()
        listed = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-Command",
                f"Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty TaskName",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert listed.stdout.strip() == ""
    finally:
        subprocess.run(
            [
                shell,
                "-NoProfile",
                "-Command",
                f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false -ErrorAction SilentlyContinue",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


def test_service_defaults_and_loopback_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BURNRATE_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert burnrate_service.DEFAULT_HOST == "127.0.0.1"
    assert burnrate_service.DEFAULT_PORT == 17331
    assert burnrate_service.DEFAULT_APP == "spend_app.api:app"
    assert burnrate_service.MUTEX_NAME == r"Local\BURNRATE-Dashboard"
    assert burnrate_service.data_root() == tmp_path / "BURNRATE"
    assert burnrate_service.pid_file() == tmp_path / "BURNRATE" / "run" / "burnrate.pid"
    assert burnrate_service.is_loopback("127.0.0.1")
    assert burnrate_service.is_loopback("localhost")
    assert not burnrate_service.is_loopback("0.0.0.0")
    assert not burnrate_service.is_loopback("*")
    args = burnrate_service.parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 17331
    assert args.app == "spend_app.api:app"
    with pytest.raises(SystemExit, match="127.0.0.1"):
        burnrate_service.serve(host="0.0.0.0", port=17331)


def test_service_write_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BURNRATE_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = burnrate_service.write_pid()
    assert path == tmp_path / "BURNRATE" / "run" / "burnrate.pid"
    assert path.read_text(encoding="utf-8") == str(os.getpid())
