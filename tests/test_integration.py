"""Настоящий процесс и корзина в изолированном XDG-каталоге."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from festyui.cli import inspect
from festyui.config import Config


@pytest.fixture
def tmp_path():
    # На том же разделе, что домашняя корзина; /tmp может быть отдельным tmpfs.
    with tempfile.TemporaryDirectory(prefix=".test-", dir=Path(__file__).resolve().parents[1]) as d:
        yield Path(d)


def test_process_check_sort_trash_restart(tmp_path):
    folder = tmp_path / "inbox"
    state = tmp_path / "state"
    cfgfile = tmp_path / "config.toml"
    cfgfile.write_text(
        f"folder={json.dumps(str(folder))}\ndata_dir={json.dumps(str(state))}\npoll_seconds=0.1\nstable_seconds=1\ncleanup_seconds=1\n[documents]\nhours=0.002\n"
    )
    cfg = Config(cfgfile)
    env = dict(os.environ, XDG_DATA_HOME=str(tmp_path / "xdg"))
    cmd = [sys.executable, "-m", "festyui.cli", "--config", str(cfgfile), "run"]

    def wait_for(condition, seconds=12):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.1)
        raise AssertionError("Timed out")

    log = (tmp_path / "process.log").open("w")
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=log)
    try:
        wait_for(
            lambda: (
                (state / "health.json").exists()
                and json.loads((state / "health.json").read_text()).get("scan")
            )
        )
        assert inspect(cfg) == 0
        (folder / "test.pdf").write_text("FEstyUI integration test, disposable")
        wait_for(lambda: (folder / "Документы/test.pdf").exists())
        # Настоящий Send2Trash, но отдельная тестовая корзина.
        wait_for(lambda: not (folder / "Документы/test.pdf").exists())
        trash = tmp_path / "xdg/Trash/files"
        assert any(p.read_text() == "FEstyUI integration test, disposable" for p in trash.iterdir())
        proc.send_signal(signal.SIGSTOP)
        health = json.loads((state / "health.json").read_text())
        health["monotonic"] -= 40
        (state / "health.json").write_text(json.dumps(health))
        assert inspect(cfg) == 1
        proc.send_signal(signal.SIGCONT)
    finally:
        proc.send_signal(signal.SIGCONT)
        proc.terminate()
        proc.wait(timeout=5)
        log.close()
    assert inspect(cfg) == 1
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(
            lambda: (
                json.loads((state / "health.json").read_text()).get("pid") == proc.pid
                and json.loads((state / "health.json").read_text()).get("scan")
            )
        )
        assert inspect(cfg) == 0
    finally:
        proc.terminate()
        proc.wait(timeout=5)
