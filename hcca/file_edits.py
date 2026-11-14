"""Exact, atomic edits to existing UTF-8 files."""

from __future__ import annotations

import codecs
import os
import stat
import tempfile
import threading
from pathlib import Path

from .errors import ToolError


_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _version(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _read_snapshot(path: Path) -> tuple[bytes, os.stat_result]:
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ToolError(f"not a regular file: {path}")
        content = source.read()
        after = os.fstat(source.fileno())
    if _version(before) != _version(after):
        raise ToolError("file changed while reading; read it again before editing")
    return content, after


def _replace_file(temporary: Path, path: Path) -> None:
    if os.name != "nt":
        os.replace(temporary, path)
        return

    # ReplaceFileW preserves the destination's Windows ACL.
    import ctypes
    from ctypes import wintypes

    replace = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    replace.restype = wintypes.BOOL
    if not replace(str(path), str(temporary), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def edit_file(path: Path, old: str, new: str) -> str:
    """Replace one unique literal match, preserving encoding and line endings.

    Uniform LF/CRLF files accept either spelling of a newline in arguments.
    Concurrent calls in this process serialize per resolved path. External
    changes are checked immediately before replacement, on a best-effort basis.
    """
    if not isinstance(old, str) or not isinstance(new, str):
        raise ToolError("old and new must be strings")
    if not old:
        raise ToolError("old must not be empty")

    path = Path(path).resolve(strict=True)
    key = os.path.normcase(str(path))
    with _PATH_LOCKS_GUARD:
        path_lock = _PATH_LOCKS.setdefault(key, threading.Lock())

    with path_lock:
        original, original_stat = _read_snapshot(path)
        has_bom = original.startswith(codecs.BOM_UTF8)
        encoding = "utf-8-sig" if has_bom else "utf-8"
        try:
            text = original.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ToolError("file is not valid UTF-8") from exc

        newline_count = text.count("\n")
        crlf_count = text.count("\r\n")
        if newline_count and crlf_count in (0, newline_count):
            newline = "\r\n" if crlf_count else "\n"
            old = old.replace("\r\n", "\n").replace("\n", newline)
            new = new.replace("\r\n", "\n").replace("\n", newline)

        position = text.find(old)
        if position < 0:
            raise ToolError("old text was not found; read the current file before retrying")
        if text.find(old, position + 1) >= 0:
            raise ToolError("old text matches multiple locations; include more context")
        updated = text[:position] + new + text[position + len(old):]
        try:
            replacement = updated.encode(encoding)
        except UnicodeEncodeError as exc:
            raise ToolError("new text cannot be encoded as UTF-8") from exc
        if replacement == original:
            return f"未修改：{path.name}（替换内容相同）"

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".hcca-edit-", suffix=".tmp",
                dir=path.parent, delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(replacement)
                output.flush()
                os.fsync(output.fileno())
            if os.name != "nt":
                os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
            os.chmod(temporary, stat.S_IMODE(original_stat.st_mode))

            current, current_stat = _read_snapshot(path)
            if (
                current != original
                or _version(current_stat) != _version(original_stat)
                or _version(path.stat()) != _version(original_stat)
            ):
                raise ToolError("file changed before editing; read it again before retrying")
            _replace_file(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                # Windows requires a writable temporary file for cleanup.
                os.chmod(temporary, stat.S_IWRITE | stat.S_IREAD)
                temporary.unlink()

        return f"已编辑：{path.name}（替换 1 处）"
