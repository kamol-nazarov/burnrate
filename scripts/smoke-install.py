"""Disposable non-editable package smoke; never starts a provider scheduler."""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-source", type=Path)
    args = parser.parse_args()
    assert sys.version_info[:2] == (3, 12), "Python 3.12 is required"
    with tempfile.TemporaryDirectory(prefix="burnrate-smoke-") as temp:
        work = Path(temp)
        env = {
            k: v
            for k, v in os.environ.items()
            if not any(
                word in k.upper()
                for word in (
                    "API_KEY",
                    "ADMIN_KEY",
                    "MANAGEMENT_KEY",
                    "SPEND_",
                    "BURNRATE",
                    "PYTHONPATH",
                )
            )
        }
        env.update(
            SPEND_DATABASE_PATH=str(work / "data" / "spend.db"),
            LOCALAPPDATA=str(work),
            HOME=str(work),
            USERPROFILE=str(work),
            PYTHONUTF8="1",
        )
        subprocess.run([sys.executable, "-m", "venv", str(work / "venv")], check=True)
        python = (
            work / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        subprocess.run(
            [str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True
        )
        if args.private_source:
            source = args.private_source.resolve()
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(source / "requirements-spend.txt"),
                ],
                check=True,
            )
            env["PYTHONPATH"] = str(source)
            env["SPEND_PRICING_PATH"] = str(source / "pricing")
        else:
            subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(work)],
                cwd=ROOT,
                check=True,
            )
            (wheel,) = work.glob("*.whl")
            subprocess.run(
                [str(python), "-m", "pip", "install", str(wheel)], cwd=work, check=True
            )

        def run(*arguments, check=True):
            return subprocess.run(
                [str(python), *arguments],
                cwd=work,
                env=env,
                check=check,
                capture_output=True,
                text=True,
            )

        doctor = run("-m", "spend_app.cli", "doctor", check=False)
        assert doctor.returncode != 0 and not Path(env["SPEND_DATABASE_PATH"]).exists()
        run("-m", "spend_app.cli", "init-db" if args.private_source else "init")
        assert json.loads(run("-m", "spend_app.cli", "doctor").stdout)["ok"]
        version = run(
            "-c", "from spend_app import __version__; print(__version__)"
        ).stdout.strip()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        code = (
            "import uvicorn; from spend_app.api import create_app; app=create_app(enable_scheduler=False); "
            "server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port="
            + str(port)
            + ")); "
            "app.add_api_route('/_smoke/stop',lambda:setattr(server,'should_exit',True),methods=['POST']); server.run()"
        )
        process = subprocess.Popen(
            [str(python), "-c", code],
            cwd=work,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    with urlopen(base + "/healthz", timeout=1) as response:
                        assert response.status == 200
                    break
                except OSError:
                    if process.poll() is not None:
                        raise AssertionError("Smoke server exited before readiness")
                    time.sleep(0.1)
            else:
                raise AssertionError("Smoke server did not become ready")
            with urlopen(base) as response:
                html = response.read().decode()
            assets = re.findall(r'(?:src|href)="(/[^"#]+)', html)
            assert any("request-state.js?v=" in asset for asset in assets)
            for asset in assets:
                with urlopen(base + asset) as response:
                    assert response.status == 200
                    expected = (
                        "javascript"
                        if ".js?" in asset
                        else "text/css"
                        if ".css?" in asset
                        else "image/svg+xml"
                    )
                    assert expected in response.headers["Content-Type"], asset
            for endpoint in (
                "/api/spend",
                "/api/spend/summary?window=1d",
                "/api/spend/health",
                "/api/subscriptions",
                "/api/onboarding",
            ):
                with urlopen(base + endpoint) as response:
                    payload = json.load(response)
                if endpoint == "/api/spend":
                    assert payload["version"] == version
                if endpoint == "/api/subscriptions" and not args.private_source:
                    assert payload["plans"] == []
                if endpoint == "/api/onboarding":
                    assert payload["hasHistory"] is False
            print(
                f"SMOKE PASS: {version}; init/readonly doctor; {len(assets)} assets; safe reads"
            )
        finally:
            try:
                with urlopen(Request(base + "/_smoke/stop", method="POST"), timeout=5):
                    pass
                process.wait(timeout=20)
                assert process.returncode == 0, "Smoke server did not shut down cleanly"
            except Exception:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                    )
                else:
                    process.kill()
                process.wait(timeout=15)
                raise


if __name__ == "__main__":
    main()
