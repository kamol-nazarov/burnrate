"""Windowless BURNRATE dashboard process (pythonw -m spend_app.service).

Scheduled-task autostart launches this module via pythonw so no console
appears. Self-heal is the task RestartCount, not a separate supervisor.
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import logging
import os
import sys
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17331
DEFAULT_APP = "spend_app.api:app"
MUTEX_NAME = r"Local\BURNRATE-Dashboard"
PID_NAME = "burnrate.pid"
ERROR_ALREADY_EXISTS = 183
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def data_root() -> Path:
    override = os.environ.get("BURNRATE_DATA_ROOT")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is not set")
    return Path(local) / "BURNRATE"


def run_dir() -> Path:
    return data_root() / "run"


def log_dir() -> Path:
    return data_root() / "logs"


def pid_file() -> Path:
    return run_dir() / PID_NAME


def ensure_directories() -> None:
    run_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)


def is_loopback(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="spend_app.service")
    parser.add_argument("--host", default=os.environ.get("BURNRATE_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BURNRATE_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument("--app", default=DEFAULT_APP)
    return parser.parse_args(argv)


def acquire_mutex(name: str = MUTEX_NAME):
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetLastError.restype = ctypes.c_uint32
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        raise OSError("CreateMutexW failed")
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    return handle


def write_pid(path: Path | None = None) -> Path:
    path = path or pid_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def _log_config(log_path: Path) -> dict:
    filename = str(log_path)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "filename": filename,
                "formatter": "default",
                "encoding": "utf-8",
            },
        },
        "root": {"handlers": ["file"], "level": "INFO"},
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
        },
    }


def configure_logging() -> Path:
    ensure_directories()
    log_path = log_dir() / "dashboard.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
        force=True,
    )
    return log_path


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    app: str = DEFAULT_APP,
) -> int:
    if not is_loopback(host):
        raise SystemExit(f"BURNRATE binds 127.0.0.1 only; refusing host {host!r}")
    if port <= 0 or port > 65535:
        raise SystemExit(f"invalid port {port}")

    ensure_directories()
    log_path = configure_logging()
    mutex = acquire_mutex()
    if mutex is False:
        logging.info("BURNRATE dashboard already running (mutex %s held)", MUTEX_NAME)
        return 0

    pid_path = write_pid()

    def _cleanup() -> None:
        try:
            if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip() == str(
                os.getpid()
            ):
                pid_path.unlink()
        except OSError:
            pass
        if mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)

    atexit.register(_cleanup)

    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=_log_config(log_path),
        access_log=False,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return serve(host=args.host, port=args.port, app=args.app)


if __name__ == "__main__":
    raise SystemExit(main())
