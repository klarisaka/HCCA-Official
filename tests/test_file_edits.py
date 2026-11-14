"""Offline regressions for exact, atomic local file edits."""

from __future__ import annotations

import codecs
import os
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hcca import file_edits
from hcca.errors import ToolError


@pytest.mark.skipif(os.name == "nt" or os.geteuid() != 0, reason="Changing ownership requires POSIX root")
def test_edit_preserves_posix_ownership(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(b"before\n")
    os.chown(path, 12345, 12346)
    os.chmod(path, 0o640)

    file_edits.edit_file(path, "before", "after")

    assert path.read_bytes() == b"after\n"
    assert (path.stat().st_uid, path.stat().st_gid) == (12345, 12346)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.parametrize("bom", [b"", codecs.BOM_UTF8])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_edit_preserves_utf8_bom_unicode_and_line_endings(
    tmp_path: Path, bom: bytes, newline: str,
) -> None:
    path = tmp_path / "中文 [sample].txt"
    path.write_bytes(bom + newline.join(["前一行", "旧内容", "末行", ""]).encode("utf-8"))

    result = file_edits.edit_file(path, "前一行\n旧内容", "前一行\n新内容\n增加一行")

    expected = newline.join(["前一行", "新内容", "增加一行", "末行", ""])
    assert path.read_bytes() == bom + expected.encode("utf-8")
    assert "替换 1 处" in result


@pytest.mark.parametrize(
    ("content", "old", "message"),
    [
        (b"first\nsecond\n", "", "must not be empty"),
        (b"first\nsecond\n", "missing", "not found"),
        (b"same\nsame\n", "same", "multiple locations"),
        (b"aaa", "aa", "multiple locations"),
        (b"\xff\xfeinvalid", "invalid", "not valid UTF-8"),
    ],
)
def test_rejected_edit_keeps_original_bytes(
    tmp_path: Path, content: bytes, old: str, message: str,
) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(content)

    with pytest.raises(ToolError, match=message):
        file_edits.edit_file(path, old, "new")

    assert path.read_bytes() == content
    assert list(tmp_path.iterdir()) == [path]


def test_delete_and_repeated_retry_report_actual_state(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(b"first\nremove\nlast\n")

    file_edits.edit_file(path, "remove\n", "")
    with pytest.raises(ToolError, match="not found"):
        file_edits.edit_file(path, "remove\n", "")

    assert path.read_bytes() == b"first\nlast\n"


def test_edit_preserves_mixed_line_endings_outside_match(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(b"first\r\nold\nlast\r\n")

    file_edits.edit_file(path, "old", "new")

    assert path.read_bytes() == b"first\r\nnew\nlast\r\n"


def test_edit_preserves_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "script.py"
    path.write_bytes(b"old\n")
    path.chmod(0o750)
    original_mode = stat.S_IMODE(path.stat().st_mode)

    file_edits.edit_file(path, "old", "new")

    assert stat.S_IMODE(path.stat().st_mode) == original_mode


@pytest.mark.skipif(os.name != "nt", reason="Windows file ACLs")
def test_edit_preserves_explicit_windows_acl(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(b"old")
    subprocess.run(
        ["icacls", str(path), "/inheritance:d"],
        check=True, capture_output=True,
    )
    before = subprocess.run(["icacls", str(path)], check=True, capture_output=True).stdout

    file_edits.edit_file(path, "old", "new")

    after = subprocess.run(["icacls", str(path)], check=True, capture_output=True).stdout
    assert after == before
    assert path.read_bytes() == b"new"


def test_failed_atomic_replacement_keeps_original_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(b"old")

    def fail_replace(temporary: Path, target: Path) -> None:
        assert temporary.parent == target.parent
        assert temporary.read_bytes() == b"new"
        assert target.read_bytes() == b"old"
        raise OSError("replacement failed")

    monkeypatch.setattr(file_edits, "_replace_file", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        file_edits.edit_file(path, "old", "new")

    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("replace_identity", [False, True])
def test_external_changes_are_detected_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace_identity: bool,
) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(b"old")
    read_snapshot = file_edits._read_snapshot
    read_count = 0

    def change_before_second_read(target: Path):
        nonlocal read_count
        read_count += 1
        if read_count == 2:
            if replace_identity:
                external = tmp_path / "external.txt"
                external.write_bytes(b"old")
                os.replace(external, path)
            else:
                path.write_bytes(b"external update")
        return read_snapshot(target)

    monkeypatch.setattr(file_edits, "_read_snapshot", change_before_second_read)

    with pytest.raises(ToolError, match="file changed"):
        file_edits.edit_file(path, "old", "new")

    assert path.read_bytes() == (b"old" if replace_identity else b"external update")
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_edits_to_same_path_both_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(b"first old\nsecond old\n")
    read_snapshot = file_edits._read_snapshot
    first_read = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    another_read = threading.Event()
    read_guard = threading.Lock()
    read_count = 0

    def pause_first_snapshot(target: Path):
        nonlocal read_count
        result = read_snapshot(target)
        with read_guard:
            read_count += 1
            is_first = read_count == 1
        if is_first:
            first_read.set()
            assert release_first.wait(5)
        else:
            another_read.set()
        return result

    def second_edit() -> str:
        second_started.set()
        return file_edits.edit_file(path.parent / "." / path.name, "second old", "second new")

    monkeypatch.setattr(file_edits, "_read_snapshot", pause_first_snapshot)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(file_edits.edit_file, path, "first old", "first new")
        try:
            assert first_read.wait(5)
            second = executor.submit(second_edit)
            assert second_started.wait(5)
            assert not another_read.wait(0.2)
        finally:
            release_first.set()
        assert "替换 1 处" in first.result(timeout=5)
        assert "替换 1 处" in second.result(timeout=5)

    assert path.read_bytes() == b"first new\nsecond new\n"


def test_unchanged_replacement_is_reported_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(b"old\n")
    original_stat = path.stat()

    result = file_edits.edit_file(path, "old", "old")

    assert "未修改" in result
    assert path.stat().st_ino == original_stat.st_ino
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
