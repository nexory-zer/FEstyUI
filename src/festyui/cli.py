"""Команды пользователя. Только run изменяет сортируемые файлы."""

import argparse
import fcntl
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .config import CATEGORIES, Config, locations
from .daemon import process_identity, run

UNIT = "festyui.service"


def inspect(cfg):
    health_path = cfg.data / "health.json"
    if not health_path.exists():
        print("Остановлена: нет отметки запущенного процесса")
        return 1
    h = json.loads(health_path.read_text())
    try:
        alive = process_identity(h["pid"]) == h["identity"]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip() == h["boot"]
    except (OSError, IndexError):
        alive = boot = False
    if not alive or not boot or h.get("stopped"):
        print("Остановлена")
        return 1
    with (cfg.data / "run.lock").open("r") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print("Остановлена: блокировка процесса отсутствует")
            return 1
        except BlockingIOError:
            pass
    age = time.monotonic() - h["monotonic"]
    print(f"Папка: {cfg.root}\nОперация: {h['operation']}\nПрогресс: {age:.1f} сек. назад")
    for field, name in [("scan", "Просмотр папки"), ("cleanup", "Проверка очистки")]:
        print(
            f"{name}: "
            + (
                time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(h[field]))
                if h[field]
                else "ещё не завершена"
            )
        )
    if age < 0 or age > 30:
        print("Нет свежего прогресса: возможно зависание")
        return 1
    cfg.validate_dirs()
    uri = (cfg.data / "state.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Повреждена база")
        counts = db.execute("SELECT state,count(*) FROM files GROUP BY state").fetchall()
        problems = db.execute(
            "SELECT path,error FROM files WHERE state='review' OR (state='active' AND error!='')"
        ).fetchall()
    print("Учёт: " + ", ".join(f"{s}={n}" for s, n in counts))
    for path, error in problems:
        print(f"Требует внимания: {path!r}: {error}")
    if h.get("error") or problems or not h["scan"]:
        print("Работает с проблемами: " + h.get("error", ""))
        return 1
    print("Работает")
    return 0


def unit_path():
    return Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user" / UNIT


def service(action, cfg):
    path = unit_path()
    if action == "install":
        path.parent.mkdir(parents=True, exist_ok=True)
        executable = str(Path(sys.executable).parent / "festyui")

        # systemd раскрывает % и $, поэтому экранируем их отдельно.
        def quote(s):
            return (
                '"'
                + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
                + '"'
            )

        text = f"""[Unit]
Description=FEstyUI temporary file sorter
StartLimitIntervalSec=120
StartLimitBurst=5
[Service]
Type=simple
ExecStart={quote(executable)} --config {quote(str(cfg.filename))} run
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
[Install]
WantedBy=default.target
"""
        if path.exists() and "Description=FEstyUI temporary file sorter" not in path.read_text():
            raise ValueError("Служба с таким именем уже существует")
        path.write_text(text)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT], check=True)
    elif action == "uninstall":
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], check=True)
        path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    else:
        subprocess.run(["systemctl", "--user", action, UNIT], check=True)


def init(filename, folder):
    filename.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# FEstyUI: после изменения настроек выполните festyui service restart",
        "folder = " + json.dumps(str(Path(folder).expanduser().absolute()), ensure_ascii=False),
        "poll_seconds = 2",
        "stable_seconds = 10",
        "cleanup_seconds = 60",
    ]
    for key, (_, extensions) in CATEGORIES.items():
        lines.extend(
            [f"\n[{key}]", "cleanup = true", "hours = 24", "extensions = " + json.dumps(extensions)]
        )
    with filename.open("x") as f:
        f.write("\n".join(lines) + "\n")
    Config(filename).validate_dirs(create=True)
    print(f"Настройки: {filename}\nПапка: {folder}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="FEstyUI — временные файлы по папкам")
    parser.add_argument("--config", type=Path, default=locations()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["run", "status", "check"]:
        commands.add_parser(name)
    scan = commands.add_parser("scan")
    scan.add_argument("--dry-run", action="store_true", required=True)
    setup = commands.add_parser("init")
    setup.add_argument("--folder", required=True)
    logs = commands.add_parser("logs")
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--follow", action="store_true")
    svc = commands.add_parser("service")
    svc.add_argument("action", choices=["install", "start", "stop", "restart", "uninstall"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "init":
            init(args.config, args.folder)
            return 0
        if args.command == "logs":
            if args.lines < 1:
                raise ValueError("Число строк должно быть положительным")
            cmd = ["journalctl", "--user", "-u", UNIT, "--no-pager", "-n", str(args.lines)]
            if args.follow:
                cmd.append("-f")
            return subprocess.call(cmd)
        cfg = Config(args.config)
        if args.command == "run":
            run(cfg)
        elif args.command in ("status", "check"):
            return inspect(cfg)
        elif args.command == "service":
            service(args.action, cfg)
        elif args.command == "scan":
            from .preview import preview

            preview(cfg)
        return 0
    except KeyboardInterrupt:
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        sqlite3.Error,
        subprocess.CalledProcessError,
    ) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
