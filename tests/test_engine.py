import json
import os
from pathlib import Path

import pytest

from festyui.cli import main
from festyui.config import Config
from festyui.daemon import Lock
from festyui.engine import Engine, signature


@pytest.fixture
def env(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("festyui.engine.time.monotonic", lambda: now[0])
    config = tmp_path / "config.toml"
    config.write_text(
        f"folder = {json.dumps(str(tmp_path / 'inbox'))}\ndata_dir = {json.dumps(str(tmp_path / 'state'))}\nstable_seconds=1\n"
    )
    cfg = Config(config)
    trash = tmp_path / "trash"
    trash.mkdir()

    def remove(path):
        src = Path(path)
        src.rename(trash / (str(len(list(trash.iterdir()))) + "_" + src.name))

    e = Engine(cfg, trash=remove, clock=lambda: now[0])
    yield e, cfg, now, trash
    e.close()


def add(e, cfg, now, name, data=b"content"):
    p = cfg.root / name
    p.write_bytes(data)
    e.tick()
    now[0] += 2
    e.tick()
    return p


@pytest.mark.parametrize(
    "name,category", [("a.PNG", "Изображения"), ("a.mp4", "Видео"), ("a.pdf", "Документы")]
)
def test_sort(env, name, category):
    e, c, n, t = env
    add(e, c, n, name)
    assert (c.root / category / name).read_bytes() == b"content"
    assert not (c.root / name).exists()


def test_ignore(env):
    e, c, n, t = env
    for name in [".secret.png", "a.exe", "a.png.part"]:
        (c.root / name).write_text("stay")
    (c.root / "nested").mkdir()
    (c.root / "nested/x.png").write_text("stay")
    (c.root / "link.png").symlink_to(c.root / "nested/x.png")
    e.tick()
    n[0] += 2
    e.tick()
    assert len(list((c.root / "Изображения").iterdir())) == 0
    assert (c.root / "link.png").is_symlink()


def test_stability(env):
    e, c, n, t = env
    p = c.root / "a.pdf"
    p.write_text("a")
    e.tick()
    n[0] += 2
    p.write_text("changed")
    e.tick()
    assert p.exists()
    n[0] += 2
    e.tick()
    assert not p.exists()


def test_duplicate_new_survives(env):
    e, c, n, t = env
    add(e, c, n, "old.png")
    old = e.store.rows("state='active'")[0]
    add(e, c, n, "new.png")
    assert not (c.root / "Изображения/old.png").exists()
    assert (c.root / "Изображения/new.png").exists()
    assert len(list(t.iterdir())) == 1
    assert e.store.rows("state='active'")[0]["deadline"] > old["deadline"]


def test_duplicate_same_name(env):
    e, c, n, t = env
    add(e, c, n, "a.png")
    add(e, c, n, "a.png")
    assert [p.name for p in (c.root / "Изображения").iterdir()] == ["a.png"]
    e.tick()
    assert [p.name for p in (c.root / "Изображения").iterdir()] == ["a.png"]


def test_different_same_name(env):
    e, c, n, t = env
    add(e, c, n, "a.png", b"one")
    add(e, c, n, "a.png", b"two")
    assert (c.root / "Изображения/a.png").read_bytes() == b"one"
    assert (c.root / "Изображения/a_2.png").read_bytes() == b"two"
    assert not list(t.iterdir())


def test_unmanaged_duplicate(env):
    e, c, n, t = env
    (c.root / "Изображения/manual.png").write_bytes(b"content")
    add(e, c, n, "new.png")
    assert (c.root / "Изображения/manual.png").exists()


def test_cleanup_and_restore(env):
    e, c, n, t = env
    add(e, c, n, "a.pdf")
    n[0] = 1000 + 86400 - 1
    e.cleanup()
    assert not list(t.iterdir())
    n[0] += 2
    e.cleanup()
    assert len(list(t.iterdir())) == 1
    next(t.iterdir()).rename(c.root / "Документы/a.pdf")
    e.cleanup()
    assert (c.root / "Документы/a.pdf").exists()


def test_modified_and_taken(env):
    e, c, n, t = env
    add(e, c, n, "a.pdf")
    add(e, c, n, "b.pdf")
    (c.root / "Документы/a.pdf").write_text("edited")
    (c.root / "Документы/b.pdf").rename(c.root.parent / "saved.pdf")
    n[0] += 90000
    e.cleanup()
    assert not list(t.iterdir())
    assert {r["state"] for r in e.store.rows()} == {"review", "taken"}


def test_trash_failure_protects_both(env):
    e, c, n, t = env
    add(e, c, n, "a.png")

    def fail(_):
        raise PermissionError("no trash")

    e.trash = fail
    add(e, c, n, "b.png")
    assert len(list((c.root / "Изображения").iterdir())) == 2
    n[0] += 90000
    e.cleanup()
    assert len(list((c.root / "Изображения").iterdir())) == 2
    (c.root / "Изображения/b.png").unlink()
    e.duplicates()
    e.cleanup()
    assert (c.root / "Изображения/a.png").exists()


def test_restart_keeps_deadline(env):
    e, c, n, t = env
    add(e, c, n, "a.pdf")
    deadline = e.store.rows()[0]["deadline"]
    other = Engine(c, trash=e.trash, clock=e.clock)
    try:
        assert other.store.rows()[0]["deadline"] == deadline
    finally:
        other.close()


def test_recover_move_both_links(env):
    e, c, n, t = env
    src = c.root / "a.pdf"
    src.write_text("test")
    dest = c.root / "Документы/a.pdf"
    ident = e.store.add(
        original="a.pdf",
        source="a.pdf",
        path="Документы/a.pdf",
        category="documents",
        sig=signature(src),
        first=n[0],
        deadline=n[0] + 86400,
        state="moving",
    )
    os.link(src, dest)
    e.recover()
    assert e.store.get(ident)["state"] == "review"
    assert src.exists() and dest.exists()


def test_recover_after_source_removed(env):
    e, c, n, t = env
    p = c.root / "Документы/a.pdf"
    p.write_text("test")
    ident = e.store.add(
        original="a.pdf",
        source="a.pdf",
        path="Документы/a.pdf",
        category="documents",
        sig=signature(p),
        first=n[0],
        deadline=n[0] + 86400,
        state="moving",
    )
    e.recover()
    assert e.store.get(ident)["state"] == "active"


def test_recover_uncertain_trash(env):
    e, c, n, t = env
    add(e, c, n, "a.pdf")
    row = e.store.rows()[0]
    e.store.update(row["id"], state="trashing")
    e.recover()
    n[0] += 90000
    e.cleanup()
    assert (c.root / "Документы/a.pdf").exists()


def test_symlink_category(env):
    e, c, n, t = env
    (c.root / "Видео").rmdir()
    (c.root / "Видео").symlink_to(t, target_is_directory=True)
    with pytest.raises(ValueError):
        e.tick()


def test_lock(env):
    e, c, n, t = env
    lock = Lock(c.data / "run.lock")
    try:
        with pytest.raises(RuntimeError):
            Lock(c.data / "run.lock")
    finally:
        lock.close()


def test_dry_run(env, capsys):
    e, c, n, t = env
    p = c.root / "a.pdf"
    p.write_text("test")
    assert main(["--config", str(c.filename), "scan", "--dry-run"]) == 0
    assert p.exists() and not e.store.rows()
    assert "Документы" in capsys.readouterr().out


def test_cleanup_disabled(env):
    e, c, n, t = env
    add(e, c, n, "a.pdf")
    c.rules["documents"]["cleanup"] = False
    n[0] += 90000
    e.cleanup()
    assert (c.root / "Документы/a.pdf").exists()
