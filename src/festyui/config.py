"""Настройки не лежат среди сортируемых файлов."""

import os
import tomllib
from pathlib import Path

CATEGORIES = {
    "images": ("Изображения", ["png", "jpg", "jpeg", "webp", "gif", "bmp", "heic"]),
    "videos": ("Видео", ["mp4", "mkv", "mov", "avi", "webm"]),
    "documents": (
        "Документы",
        ["pdf", "doc", "docx", "txt", "odt", "rtf", "xls", "xlsx", "csv", "ppt", "pptx"],
    ),
}


def locations():
    home = Path.home()
    return (
        Path(os.getenv("XDG_CONFIG_HOME", home / ".config")) / "festyui/config.toml",
        Path(os.getenv("XDG_DATA_HOME", home / ".local/share")) / "festyui",
    )


class Config:
    def __init__(self, filename):
        self.filename = Path(filename).absolute()
        with self.filename.open("rb") as f:
            raw = tomllib.load(f)
        self.root = Path(raw["folder"]).expanduser()
        if not self.root.is_absolute() or self.root in (Path("/"), Path.home()):
            raise ValueError("Нужна отдельная абсолютная папка FEstyUI")
        self.data = Path(raw.get("data_dir", str(locations()[1]))).expanduser()
        if not self.data.is_absolute() or self.data.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("База должна находиться вне сортируемой папки")
        self.poll = float(raw.get("poll_seconds", 2))
        self.stable = float(raw.get("stable_seconds", 10))
        self.cleanup_interval = float(raw.get("cleanup_seconds", 60))
        if not (
            0.1 <= self.poll <= 10
            and 1 <= self.stable <= 3600
            and 1 <= self.cleanup_interval <= 3600
        ):
            raise ValueError("Недопустимые интервалы")
        self.rules = {}
        self.extensions = {}
        for key, (directory, defaults) in CATEGORIES.items():
            rule = raw.get(key, {})
            hours = float(rule.get("hours", 24))
            enabled = rule.get("cleanup", True)
            if not 0 < hours <= 87600 or not isinstance(enabled, bool):
                raise ValueError("Проверьте hours и cleanup")
            self.rules[key] = {"dir": directory, "hours": hours, "cleanup": enabled}
            for ext in rule.get("extensions", defaults):
                ext = ext.lower().lstrip(".")
                if not ext.isalnum() or ext in self.extensions:
                    raise ValueError("Некорректное или повторное расширение")
                self.extensions[ext] = key

    def category(self, path):
        if path.name.startswith("."):
            return None
        return self.extensions.get(path.suffix.lower().lstrip("."))

    def validate_dirs(self, create=False):
        paths = [self.root] + [self.root / r["dir"] for r in self.rules.values()]
        for path in paths:
            if create:
                path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or path.resolve() != path.absolute() or not path.is_dir():
                raise ValueError(f"Папка отсутствует или содержит ссылку: {path}")
            if path.stat().st_dev != self.root.stat().st_dev:
                raise ValueError("Подпапки должны быть на том же диске")
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                raise PermissionError(f"Нет доступа: {path}")
