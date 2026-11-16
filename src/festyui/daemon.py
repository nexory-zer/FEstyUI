"""Фоновый процесс и отметки реального продвижения работы."""

import fcntl
import json
import logging
import os
import signal
import time
import uuid
from pathlib import Path

from .engine import Engine


class Lock:
    def __init__(self, path):
        self.file = open(path, "a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("FEstyUI уже запущена") from None

    def close(self):
        self.file.close()


def process_identity(pid):
    text = Path(f"/proc/{pid}/stat").read_text()
    return text[text.rfind(")") + 2 :].split()[19]


def run(cfg):
    os.umask(0o077)
    cfg.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = Lock(cfg.data / "run.lock")
    stop = False

    def halt(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)
    health = {
        "pid": os.getpid(),
        "identity": process_identity(os.getpid()),
        "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "run_id": str(uuid.uuid4()),
        "scan": None,
        "cleanup": None,
        "error": "",
    }
    last_write = 0

    def pulse(operation, force=False):
        nonlocal last_write
        mono = time.monotonic()
        if not force and mono - last_write < 1:
            return
        health.update(operation=operation, monotonic=mono, timestamp=time.time())
        tmp = cfg.data / "health.tmp"
        tmp.write_text(json.dumps(health, ensure_ascii=False))
        tmp.replace(cfg.data / "health.json")
        last_write = mono

    engine = None
    try:
        pulse("Запуск", True)
        engine = Engine(cfg, pulse=pulse)
        logging.info("FEstyUI запущена. Папка: %s", cfg.root)
        while not stop:
            try:
                engine.errors.pop("Цикл", None)
                engine.tick()
                health.update(
                    scan=time.time(),
                    cleanup=engine.last_cleanup,
                    error="; ".join(engine.errors.values()),
                )
            except (OSError, ValueError) as e:
                engine.report("Цикл", e)
                health["error"] = str(e)
            pulse("Ожидание" if not health["error"] else "Ошибка", True)
            deadline = time.monotonic() + cfg.poll
            while not stop and time.monotonic() < deadline:
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    finally:
        if engine:
            engine.close()
        health["stopped"] = True
        pulse("Остановлена", True)
        lock.close()
        logging.info("FEstyUI остановлена")
