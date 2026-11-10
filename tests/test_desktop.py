import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from app import desktop
from app.config import runtime_instance_id

ROOT = Path(__file__).resolve().parents[1]


def test_repeated_launch_reuses_only_the_same_project(monkeypatch):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        own = (200, {"application": "dovod", "instance_id": runtime_instance_id()})
        monkeypatch.setattr(desktop, "local_json", lambda _: own)
        assert desktop.reserve_port(port) == (None, port)
        monkeypatch.setattr(desktop, "local_json", lambda _: (200, {
            "application": "dovod", "instance_id": "another-folder",
        }))
        listener, selected = desktop.reserve_port(port)
        try:
            assert listener is not None and selected != port
        finally:
            listener.close()


def test_health_identity_requires_dovod_and_the_current_folder():
    assert not desktop.is_own_server((200, ["unexpected"]))
    assert not desktop.is_own_server((200, {"instance_id": runtime_instance_id()}))
    assert not desktop.is_own_server(None)


def test_smoke_starts_and_stops_real_local_server(tmp_path):
    env = {**os.environ, "DATABASE_URL": "", "DATABASE_PATH": str(tmp_path / "zip.sqlite3"),
           "GIGACHAT_AUTH_KEY": "", "PARSER_API_KEY": "", "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "app.desktop", "--port", "0", "--no-browser", "--smoke-test"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=35,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Довод готов: http://127.0.0.1:" in result.stdout
    assert (tmp_path / "zip.sqlite3").exists()


def test_zip_readme_copy_and_bootstrap_manifest():
    assert (ROOT / "README.md").read_text(encoding="utf-8") == (
        ROOT / "README.txt").read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "scripts/windows-python.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "3.14.10"
    assert len(manifest["sha256"]) == 64
    assert manifest["url"].startswith("https://www.python.org/ftp/python/")
