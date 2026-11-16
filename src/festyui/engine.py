"""Один последовательный цикл: сначала сохранить, затем убирать старое."""

import hashlib
import json
import logging
import os
import stat
import time

from send2trash import send2trash

from .storage import Store

log = logging.getLogger("festyui")


def signature(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode):
        raise ValueError("Не обычный файл")
    return json.dumps([s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns])


def matches(path, sig):
    try:
        return signature(path) == sig
    except (OSError, ValueError):
        return False


def safe_open(path):
    return os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")


class Engine:
    def __init__(self, cfg, trash=send2trash, clock=time.time, pulse=lambda operation: None):
        self.cfg, self.trash, self.clock, self.pulse = cfg, trash, clock, pulse
        cfg.validate_dirs(create=True)
        cfg.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(cfg.data / "state.sqlite3")
        self.waiting = {}
        self.errors = {}
        self.last_cleanup = None
        self.recover()

    def close(self):
        self.store.db.close()

    def path(self, relative):
        p = self.cfg.root / relative
        if p.parent not in [
            self.cfg.root,
            *[self.cfg.root / r["dir"] for r in self.cfg.rules.values()],
        ]:
            raise ValueError("Путь за пределами разрешённых папок")
        return p

    def report(self, key, error):
        text = str(error)
        if self.errors.get(key) != text:
            log.error("%s: %s", key, text)
        self.errors[key] = text

    def recover(self):
        # Не повторяем неясное удаление: восстановленный из корзины файл сохраняем.
        for row in self.store.rows("state IN ('moving','trashing')"):
            dest = self.path(row["path"])
            if row["state"] == "trashing":
                self.store.update(
                    row["id"],
                    state="review" if dest.exists() else "trashed",
                    error="Проверить после прерванной очистки" if dest.exists() else "",
                )
            else:
                source = self.path(row["source"])
                if matches(dest, row["sig"]) and not source.exists():
                    self.store.update(row["id"], state="active")
                else:
                    self.store.update(
                        row["id"],
                        state="review",
                        error="Прерван перенос; файлы оставлены для проверки",
                    )

    def fingerprint(self, path):
        before = signature(path)
        h = hashlib.sha256()
        with safe_open(path) as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
                self.pulse("Сравнение изображения")
        if not matches(path, before):
            raise ValueError("Файл изменился во время чтения")
        return h.hexdigest()

    def equal(self, a, b, sa, sb):
        if not matches(a, sa) or not matches(b, sb):
            return False
        with safe_open(a) as fa, safe_open(b) as fb:
            while True:
                aa, bb = fa.read(1024 * 1024), fb.read(1024 * 1024)
                self.pulse("Побайтовое сравнение")
                if aa != bb:
                    return False
                if not aa:
                    break
        return matches(a, sa) and matches(b, sb)

    def available(self, directory, name):
        p = directory / name
        index = 2
        while os.path.lexists(p):
            p = directory / f"{os.path.splitext(name)[0]}_{index}{os.path.splitext(name)[1]}"
            index += 1
        return p

    def move(self, row, dest):
        src = self.path(row["path"])
        if not matches(src, row["sig"]):
            raise ValueError("Источник изменился")
        self.store.update(
            row["id"],
            state="moving",
            source=str(src.relative_to(self.cfg.root)),
            path=str(dest.relative_to(self.cfg.root)),
        )
        # hard link создаёт назначение без перезаписи; обе папки на одном диске.
        os.link(src, dest, follow_symlinks=False)
        if not matches(src, row["sig"]) or not matches(dest, row["sig"]):
            self.store.update(row["id"], state="review", error="Изменение во время переноса")
            return
        src.unlink()
        self.store.update(row["id"], state="active", error="")
        log.info("Перенесён %r → %r", src.name, str(dest.relative_to(self.cfg.root)))

    def scan(self):
        now = self.clock()
        for src in sorted(self.cfg.root.iterdir()):
            category = self.cfg.category(src)
            if not category:
                continue
            try:
                sig = signature(src)
            except (OSError, ValueError):
                continue
            existing = self.store.rows(
                "path=? AND state IN ('waiting','review','moving')", (src.name,)
            )
            if existing and existing[-1]["state"] != "waiting":
                continue
            if existing:
                row = existing[-1]
                if row["sig"] != sig:
                    if json.loads(row["sig"])[:2] != json.loads(sig)[:2]:
                        self.store.update(
                            row["id"],
                            sig=sig,
                            first=now,
                            deadline=now + self.cfg.rules[category]["hours"] * 3600,
                        )
                    else:
                        self.store.update(row["id"], sig=sig)
                    row = self.store.get(row["id"])
            else:
                ident = self.store.add(
                    original=src.name,
                    source=src.name,
                    path=src.name,
                    category=category,
                    sig=sig,
                    first=now,
                    deadline=now + self.cfg.rules[category]["hours"] * 3600,
                    state="waiting",
                )
                row = self.store.get(ident)
            previous = self.waiting.get(row["id"])
            mono = time.monotonic()
            if previous is None or previous[0] != sig:
                self.waiting[row["id"]] = (sig, mono)
                continue
            if mono - previous[1] < self.cfg.stable:
                continue
            try:
                if now >= row["deadline"]:
                    self.store.update(
                        row["id"], state="review", error="Срок истёк до окончания копирования"
                    )
                    continue
                digest = self.fingerprint(src) if category == "images" else None
                self.store.update(row["id"], digest=digest, sig=sig)
                dest = self.available(self.cfg.root / self.cfg.rules[category]["dir"], src.name)
                self.move(self.store.get(row["id"]), dest)
                self.waiting.pop(row["id"], None)
                self.errors.pop(src.name, None)
            except (OSError, ValueError) as e:
                self.report(src.name, e)
                if self.store.get(row["id"])["state"] == "moving":
                    self.store.update(row["id"], state="review", error=str(e))
        self.pulse("Просмотр папки завершён")

    def verified(self, row):
        path = self.path(row["path"])
        if not os.path.lexists(path):
            self.store.update(row["id"], state="taken", error="")
            return False
        if not matches(path, row["sig"]):
            self.store.update(row["id"], state="review", error="Файл изменён или подменён")
            return False
        return True

    def remove(self, row):
        self.cfg.validate_dirs()
        if not self.verified(row):
            return False
        self.store.update(row["id"], state="trashing", error="")
        try:
            self.trash(str(self.path(row["path"])))
        except OSError as e:
            # Результат ошибки может быть неоднозначным; отсутствующий файл не повторяем.
            state = "active" if matches(self.path(row["path"]), row["sig"]) else "review"
            self.store.update(row["id"], state=state, error=str(e))
            self.report(row["path"], e)
            return False
        self.store.update(row["id"], state="trashed", error="")
        self.errors.pop(row["path"], None)
        log.info("В корзину: %r", row["path"])
        return True

    def duplicates(self):
        rows = self.store.rows("state='active' AND category='images'")
        for old in rows:
            if not self.verified(old):
                continue
            candidates = [r for r in rows if r["id"] > old["id"] and r["digest"] == old["digest"]]
            for new in reversed(candidates):
                new = self.store.get(new["id"])
                if new["state"] != "active" or not self.verified(new):
                    continue
                if self.equal(
                    self.path(old["path"]), self.path(new["path"]), old["sig"], new["sig"]
                ):
                    self.store.update(old["id"], replacement=new["id"])
                    if self.remove(self.store.get(old["id"])):
                        log.info("Повторное фото: оставлена новая копия %r", new["path"])
                    break
        # Вернуть входящее имя после успешного удаления старой копии.
        for row in self.store.rows("state='active' AND category='images'"):
            target = self.path(row["path"]).parent / PathName(row["original"])
            if (
                target != self.path(row["path"])
                and not os.path.lexists(target)
                and self.verified(row)
            ):
                self.move(row, target)

    def cleanup(self):
        rows = self.store.rows("state='active'")
        protected = set()
        for row in rows:
            if row["replacement"]:
                protected.update([row["id"], row["replacement"]])
        for row in rows:
            if row["id"] in protected or not self.verified(row):
                continue
            if self.cfg.rules[row["category"]]["cleanup"] and self.clock() >= row["deadline"]:
                self.remove(row)
        self.last_cleanup = self.clock()
        self.pulse("Проверка очистки завершена")

    def tick(self):
        self.cfg.validate_dirs()
        self.scan()
        self.duplicates()
        if (
            self.last_cleanup is None
            or self.clock() - self.last_cleanup >= self.cfg.cleanup_interval
        ):
            self.cleanup()


def PathName(value):
    from pathlib import Path

    return Path(value).name
