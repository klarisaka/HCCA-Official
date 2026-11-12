"""Practical tools for HCCA agents: web search/fetch and workspace operations.

The collaboration primitives (SEND_MESSAGE / SPAWN / WAIT / SUBMIT ...) are the
*control* plane.  This module is the *information* plane: the tools an agent can
actually run to bring outside evidence in and to operate on a workspace.

Design constraints:

* No extra dependencies -- ``urllib`` only, so a bare Python install works.
* Every result is plain text, already trimmed to a token-reasonable size; the
  agent never receives raw HTML or an unbounded command log.
* Workspace tools are rooted at one directory: path escapes are rejected and
  commands run with that directory as their cwd (and a hard timeout).
* ``web_search`` is best-effort: DuckDuckGo HTML first, Bing RSS as fallback.
  A failed search returns an error string, never raises into the agent loop.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ToolError

__all__ = ["AgentToolbox", "TOOL_NAMES"]

TOOL_NAMES = (
    "web_search",
    "web_fetch",
    "read_file",
    "write_file",
    "list_files",
    "search_files",
    "run_command",
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
}

_TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".rst",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".csv",
    ".log",
}

_BLOCK_TAGS = re.compile(r"<(script|style|noscript|svg|head)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\x0b\f\r]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def _ssl_context() -> ssl.SSLContext:
    """Windows Python does not use the OS trust store; prefer certifi if installed."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - certifi is optional
        return ssl.create_default_context()


def _open(request: urllib.request.Request, *, timeout: float) -> tuple[Any, bool]:
    """urlopen with a usable CA bundle; one unverified retry on cert failures.

    Returns ``(response, insecure_retry)``. Some corporate/hospital networks
    intercept TLS with a private CA, so a hard failure would make web search
    useless; the retry is surfaced in the tool output instead of hidden.
    """
    try:
        return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()), False
    except urllib.error.URLError as exc:
        message = str(exc).lower()
        if "certificate_verify_failed" not in message and "certificate verify failed" not in message:
            raise
        context = ssl._create_unverified_context()
        return urllib.request.urlopen(request, timeout=timeout, context=context), True


def _strip_html(document: str) -> str:
    document = _BLOCK_TAGS.sub(" ", document)
    text = _TAG.sub(" ", document)
    text = html_lib.unescape(text)
    text = _SPACES.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return _BLANK_LINES.sub("\n\n", text).strip()


def _decode_body(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1)
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _terminate_process_tree(proc: subprocess.Popen[Any]) -> None:
    """Kill a command *and* its grandchildren.

    ``subprocess.run(timeout=...)`` only kills the direct child and then keeps
    waiting for the stdout pipe to close.  A command that starts a detached
    background process (``Start-Process``/``nohup``) leaves a grandchild holding
    that pipe, so the timeout never returns: one real run had a single tool call
    stuck for 24+ minutes while the model looked frozen.  Killing the whole tree
    is what actually ends the tool call.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


class AgentToolbox:
    """A small, root-bound toolset shared by all agents of one HCCA run."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        timeout: float = 60.0,
        max_chars: int = 6000,
        allow_web: bool = True,
        allow_run: bool = True,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ToolError(f"workspace does not exist: {self.workspace}")
        self.timeout = float(timeout)
        self.max_chars = int(max_chars)
        self.allow_web = bool(allow_web)
        self.allow_run = bool(allow_run)
        #: Per-call audit log (name, ok, chars, duration) used by status.json.
        self.log: list[dict[str, Any]] = []
        self._counts: dict[str, int] = {name: 0 for name in TOOL_NAMES}

    # -- schemas -----------------------------------------------------------
    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style function schemas, restricted to the enabled tools."""
        tools = [
            self._fn(
                "web_search",
                "联网搜索：返回标题、URL 和摘要。用于核实事实、找时间线/数据/来源。"
                "搜索后对关键页面用 web_fetch 打开原文核实；引用时写出 URL。",
                {
                    "query": {"type": "string", "description": "搜索词，尽量具体"},
                    "max_results": {
                        "type": "integer",
                        "description": "返回条数，默认 5，最多 10",
                    },
                },
                ["query"],
            ),
            self._fn(
                "web_fetch",
                "抓取一个网页并抽取正文纯文本（去脚本/样式）。用于核对搜索结果的原文。",
                {
                    "url": {"type": "string", "description": "http(s) URL"},
                    "max_chars": {
                        "type": "integer",
                        "description": "最多返回字符数，默认 6000",
                    },
                },
                ["url"],
            ),
            self._fn(
                "read_file",
                "读取工作区内的文本文件（可指定起止行）。",
                {
                    "path": {"type": "string", "description": "相对工作区的路径"},
                    "start_line": {"type": "integer", "description": "起始行，1 起"},
                    "end_line": {"type": "integer", "description": "结束行，含"},
                },
                ["path"],
            ),
            self._fn(
                "write_file",
                "把文本写入工作区内的文件（自动创建父目录，覆盖已有内容）。"
                "用于交付结果文件。",
                {
                    "path": {"type": "string", "description": "相对工作区的路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                ["path", "content"],
            ),
            self._fn(
                "list_files",
                "列出工作区内某个目录的文件与子目录。",
                {
                    "path": {"type": "string", "description": "相对路径，默认 ."},
                    "pattern": {
                        "type": "string",
                        "description": "可选 glob 过滤，如 *.py",
                    },
                },
                ["path"],
            ),
            self._fn(
                "search_files",
                "在工作区文本文件中按正则搜索，返回每条匹配行的 file:line: 片段。",
                {
                    "pattern": {"type": "string", "description": "Python 正则"},
                    "path": {"type": "string", "description": "限定目录，默认 ."},
                    "max_results": {"type": "integer", "description": "所有文件合计的匹配行上限，默认 40，最大 200"},
                    "context_lines": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "每条匹配前后的源码行数，默认 0；同文件重叠或相邻范围合并，受总返回字符预算限制",
                    },
                },
                ["pattern"],
            ),
            self._fn(
                "run_command",
                "在【工作区根目录】执行一条命令并返回输出尾部。Windows 用 PowerShell，"
                "Linux 用 Bash（pipefail），其他平台用 /bin/sh。适合运行测试、脚本、索引。高风险操作不要执行。",
                {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "超时秒数，默认 60，最大 300",
                    },
                },
                ["command"],
            ),
        ]
        if not self.allow_web:
            tools = [tool for tool in tools if tool["function"]["name"] not in ("web_search", "web_fetch")]
        if not self.allow_run:
            tools = [tool for tool in tools if tool["function"]["name"] != "run_command"]
        return tools

    @staticmethod
    def _fn(
        name: str,
        description: str,
        properties: Mapping[str, Any],
        required: Sequence[str],
    ) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": dict(properties),
                    "required": list(required),
                    "additionalProperties": False,
                },
            },
        }

    # -- dispatch ----------------------------------------------------------
    def call(self, name: str, arguments: Mapping[str, Any] | None) -> str:
        """Run one tool call; never raises -- failures become ``ERROR: ...``."""
        args = dict(arguments or {})
        started = time.time()
        ok = True
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                raise ToolError(f"unknown tool: {name}")
            if name in ("web_search", "web_fetch") and not self.allow_web:
                raise ToolError("web tools are disabled for this run")
            if name == "run_command" and not self.allow_run:
                raise ToolError("run_command is disabled for this run")
            result = handler(args)
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001 - the agent must see the failure
            ok = False
            result = f"ERROR: {type(exc).__name__}: {exc}"
        self._counts[name] = self._counts.get(name, 0) + 1
        self.log.append(
            {
                "tool": name,
                "ok": ok,
                "chars": len(result),
                "duration_ms": int((time.time() - started) * 1000),
                "args": _summarise_args(args),
            }
        )
        return result[: self.max_chars * 2]

    # -- web ---------------------------------------------------------------
    def _backends(self, query: str) -> list[Any]:
        """Backend order by query script / network reachability.

        DDG is usually unreachable from mainland China and Bing occasionally
        answers CJK queries with unrelated pages, so Chinese queries start at
        Sogou; English queries start at Bing (fast, good recall).
        """
        if _has_cjk(query):
            return [self._search_sogou, self._search_so360, self._search_bing, self._search_ddg]
        return [self._search_bing, self._search_so360, self._search_sogou, self._search_ddg]

    def _tool_web_search(self, args: Mapping[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("query is required")
        limit = max(1, min(10, _as_int(args.get("max_results"), 5)))
        errors: list[str] = []
        for attempt, candidate in enumerate((query, _simplify_query(query))):
            if attempt and candidate == query:
                continue
            for backend in self._backends(candidate):
                try:
                    results = backend(candidate, limit)
                except Exception as exc:  # noqa: BLE001 - try the next backend
                    errors.append(f"{backend.__name__}: {type(exc).__name__}: {exc}")
                    continue
                if not results:
                    errors.append(f"{backend.__name__}: no results")
                    continue
                if not _looks_relevant(candidate, results):
                    errors.append(f"{backend.__name__}: {len(results)} 条结果与查询无关")
                    continue
                lines = [f"搜索：{candidate}（{len(results)} 条，{backend.__name__[8:]}）"]
                for index, item in enumerate(results, 1):
                    lines.append(f"{index}. {item['title']}\n   {item['url']}\n   {item['snippet']}")
                return "\n".join(lines)
        return (
            "ERROR: web_search 未获得相关结果（网络受限或搜索引擎拒绝）。"
            "可以换更短的关键词/英文关键词再试一次；仍失败就基于已有知识作答并标注「未核实」。\n"
            + (" | ".join(errors[-6:]) if errors else "")
        )

    def _get_text(self, url: str, *, accept: str = "text/html") -> str:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": accept,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        with _open(request, timeout=min(20.0, self.timeout))[0] as response:
            return _decode_body(response.read(), response.headers.get("Content-Type", ""))

    def _parse_result_blocks(
        self,
        document: str,
        block_pattern: str,
        *,
        limit: int,
        base_url: str = "",
        title_link_pattern: str = r'<h[23][^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    ) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for block in re.findall(block_pattern, document, re.S):
            match = re.search(title_link_pattern, block, re.S)
            if not match:
                continue
            url = _normalise_url(match.group(1), base_url)
            title = _strip_html(match.group(2))
            if not title or not url:
                continue
            snippet = _strip_html(block[match.end() :])[:280]
            results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= limit:
                break
        return results

    def _search_bing(self, query: str, limit: int) -> list[dict[str, str]]:
        url = "https://www.bing.com/search?" + urllib.parse.urlencode(
            {"q": query, "mkt": "zh-CN" if _has_cjk(query) else "en-US"}
        )
        document = self._get_text(url)
        return self._parse_result_blocks(document, r'<li class="b_algo".*?</li>', limit=limit)

    def _search_sogou(self, query: str, limit: int) -> list[dict[str, str]]:
        url = "https://www.sogou.com/web?" + urllib.parse.urlencode({"query": query, "ie": "utf8"})
        document = self._get_text(url)
        return self._parse_result_blocks(
            document,
            r'<div class="vrwrap".*?(?=<div class="vrwrap"|<div id="pagebar|<div class="pager|</body>)',
            limit=limit,
            base_url="https://www.sogou.com",
        )

    def _search_so360(self, query: str, limit: int) -> list[dict[str, str]]:
        url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": query})
        document = self._get_text(url)
        return self._parse_result_blocks(
            document,
            r'<li class="res-list".*?</li>',
            limit=limit,
            base_url="https://www.so.com",
        )

    def _search_ddg(self, query: str, limit: int) -> list[dict[str, str]]:
        body = urllib.parse.urlencode({"q": query, "kl": "wt-wt"}).encode()
        request = urllib.request.Request(
            "https://html.duckduckgo.com/html/",
            data=body,
            headers={
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html",
            },
            method="POST",
        )
        with _open(request, timeout=min(20.0, self.timeout))[0] as response:
            document = _decode_body(response.read(), response.headers.get("Content-Type", ""))
        results: list[dict[str, str]] = []
        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
            r'(?:class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?',
            re.S,
        )
        for match in pattern.finditer(document):
            url = _ddg_url(match.group("href"))
            title = _strip_html(match.group("title") or "")
            snippet = _strip_html(match.group("snippet") or "")
            if title and url:
                results.append({"title": title, "url": url, "snippet": snippet[:300]})
            if len(results) >= limit:
                break
        return results

    def _tool_web_fetch(self, args: Mapping[str, Any]) -> str:
        url = str(args.get("url") or "").strip()
        if not url:
            raise ToolError("url is required")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ToolError(f"unsupported URL scheme: {parsed.scheme!r}")
        limit = max(500, min(_as_int(args.get("max_chars"), self.max_chars), 20000))
        request = urllib.request.Request(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"},
        )
        response, insecure = _open(request, timeout=min(30.0, self.timeout))
        with response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(3_000_000)
            final_url = response.geturl()
        document = _decode_body(raw, content_type)
        if "html" in content_type.lower() or "<html" in document[:2000].lower():
            text = _strip_html(document)
        else:
            text = document.strip()
        text = _SPACES.sub(" ", text)
        note = ""
        if insecure:
            note += "\n\n[注意：TLS 证书校验失败，本次抓取使用不校验模式，来源可信度需自行判断]"
        if len(text) > limit:
            text = text[:limit]
            note = f"\n\n…（已截断，原文 {len(document)} 字符；可增大 max_chars 或用 read_file 处理保存到工作区的文件）" + note
        return f"来源：{final_url}\n\n{text}{note}"

    # -- workspace ---------------------------------------------------------
    def _resolve(self, rel_path: str) -> Path:
        candidate = (self.workspace / (rel_path or ".")).resolve()
        if candidate != self.workspace and not candidate.is_relative_to(self.workspace):
            raise ToolError(f"path escapes workspace: {rel_path}")
        return candidate

    def _tool_read_file(self, args: Mapping[str, Any]) -> str:
        path = self._resolve(str(args.get("path") or ""))
        if not path.is_file():
            raise ToolError(f"missing file: {args.get('path')}")
        text = path.read_text(encoding="utf-8", errors="replace")
        start = _as_int(args.get("start_line"), 0)
        end = _as_int(args.get("end_line"), 0)
        if start or end:
            lines = text.split("\n")
            first = max(1, start or 1)
            last = min(len(lines), end or len(lines))
            text = "\n".join(f"{number:>5}  {line}" for number, line in enumerate(lines[first - 1 : last], first))
        limit = self.max_chars * 2
        if len(text) > limit:
            text = text[:limit] + f"\n…（已截断，共 {len(text)} 字符；请用 start_line/end_line 分段读取）"
        return f"文件：{path.relative_to(self.workspace).as_posix()}\n\n{text}"

    def _tool_write_file(self, args: Mapping[str, Any]) -> str:
        path = self._resolve(str(args.get("path") or ""))
        if path == self.workspace or path.is_dir():
            raise ToolError(f"not a file path: {args.get('path')}")
        content = str(args.get("content") or "")
        if len(content) > 200_000:
            raise ToolError("content too large (200000 chars max)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return (
            f"已写入：{path.relative_to(self.workspace).as_posix()}"
            f"（{len(content)} 字符 / {path.stat().st_size} 字节）"
        )

    def _tool_list_files(self, args: Mapping[str, Any]) -> str:
        path = self._resolve(str(args.get("path") or "."))
        if not path.is_dir():
            raise ToolError(f"not a directory: {args.get('path')}")
        pattern = str(args.get("pattern") or "")
        entries: list[str] = []
        skipped: list[str] = []
        if pattern:
            try:
                candidates = sorted(path.glob(pattern))
            except OSError as exc:  # broken junction / vanished directory
                raise ToolError(f"cannot glob {args.get('pattern')!r}: {exc}") from exc
        else:
            try:
                candidates = sorted(path.iterdir())
            except OSError as exc:
                raise ToolError(f"cannot list {args.get('path')!r}: {exc}") from exc
        for item in candidates[:200]:
            if item.name in _IGNORED_DIRS:
                continue
            try:
                if item.is_dir():
                    suffix = "/"
                else:
                    suffix = f"  {item.stat().st_size} B"
            except OSError:
                # A broken link or a path that vanished between list and stat:
                # keep the entry, only drop its size.
                skipped.append(item.name)
                suffix = "  ? B"
            entries.append(f"{item.relative_to(self.workspace).as_posix()}{suffix}")
        header = f"目录：{path.relative_to(self.workspace).as_posix() or '.'}（{len(entries)} 项）"
        note = f"\n（{len(skipped)} 个条目不可读，已跳过：{', '.join(skipped[:5])}）" if skipped else ""
        return header + "\n" + "\n".join(entries[:200]) + note

    def _iter_files(self, base: Path) -> tuple[list[Path], list[str]]:
        """List files under ``base`` without ever aborting on a bad directory.

        ``Path.rglob`` raises ``FileNotFoundError``/``OSError`` when a directory
        disappears or a Windows junction is broken in the middle of the walk.
        That used to fail the whole search, which made agents conclude that the
        workspace was empty (observed in a real run).  ``os.walk`` with an
        ``onerror`` hook keeps scanning the healthy part of the tree.
        """
        if not base.is_dir():
            return [base], []
        files: list[Path] = []
        skipped: list[str] = []

        def _onerror(exc: OSError) -> None:  # pragma: no cover - environment specific
            skipped.append(str(getattr(exc, "filename", "") or exc))

        for root, dirs, names in os.walk(base, onerror=_onerror, followlinks=False):
            dirs[:] = [name for name in dirs if name not in _IGNORED_DIRS]
            for name in names:
                files.append(Path(root) / name)
        return files, skipped

    def _tool_search_files(self, args: Mapping[str, Any]) -> str:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            raise ToolError("pattern is required")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regex: {exc}") from exc
        base = self._resolve(str(args.get("path") or "."))
        limit = max(1, min(200, _as_int(args.get("max_results"), 40)))
        context_lines = max(0, _as_int(args.get("context_lines"), 0))
        hits: list[str] = []
        contexts: list[tuple[str, int, int]] = []
        files, skipped = self._iter_files(base)
        for path in files:
            try:
                if not path.is_file():
                    continue
                if any(part in _IGNORED_DIRS for part in path.parts):
                    continue
                if path.suffix.lower() not in _TEXT_SUFFIXES:
                    continue
                if path.stat().st_size > 512 * 1024:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.split("\n"), 1):
                if regex.search(line):
                    try:
                        rel = path.relative_to(self.workspace).as_posix()
                    except ValueError:
                        rel = str(path)
                    hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                    if context_lines:
                        first = max(1, number - context_lines)
                        last = number + context_lines
                        if contexts and contexts[-1][0] == rel and first <= contexts[-1][2] + 1:
                            contexts[-1] = (rel, contexts[-1][1], last)
                        else:
                            contexts.append((rel, first, last))
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        note = f"\n（另跳过 {len(skipped)} 个不可读目录，检索结果不覆盖其内容）" if skipped else ""
        if not hits:
            return f"未找到匹配：{pattern!r}{note}"
        result = f"匹配 {pattern!r}：{len(hits)} 行匹配\n" + "\n".join(hits)
        if not context_lines:
            return result + note
        budget = self.max_chars * 2 - len(note)
        result += "\n\n上下文："
        for rel, first, last in contexts:
            if len(result) >= budget:
                break
            try:
                context = self._tool_read_file({"path": rel, "start_line": first, "end_line": last})
            except (OSError, ToolError) as exc:
                context = f"（部分上下文不可读：{rel}：{exc}）"
            result += "\n\n" + context
            if len(result) > budget:
                break
        else:
            return result + note
        truncated = f"\n…（已截断，{len(hits)} 行匹配）"
        return result[: max(0, budget - len(truncated))] + truncated + note

    def _tool_run_command(self, args: Mapping[str, Any]) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ToolError("command is required")
        timeout = max(1, min(300, _as_int(args.get("timeout_seconds"), int(self.timeout))))
        if sys.platform == "win32":
            workspace_literal = str(self.workspace).replace("'", "''")
            # PowerShell's startup location can fall back to System32 when the
            # process cwd contains wildcard characters such as brackets.
            shell_command = (
                f"Set-Location -LiteralPath '{workspace_literal}' -ErrorAction Stop\n"
                "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
                "$OutputEncoding = [Console]::OutputEncoding\n"
                f"{command}"
            )
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", shell_command]
        elif sys.platform == "linux":
            argv = ["/bin/bash", "-o", "pipefail", "-c", command]
        else:
            argv = ["/bin/sh", "-c", command]
        started = time.time()
        # Redirect into a temp file instead of a pipe: a detached grandchild
        # can inherit the pipe and keep it open forever, which makes even a
        # timed-out ``subprocess.run`` block when it waits for EOF.
        with tempfile.TemporaryFile() as sink:
            popen_kwargs: dict[str, Any] = {
                "cwd": str(self.workspace),
                "stdin": subprocess.DEVNULL,
                "stdout": sink,
                "stderr": subprocess.STDOUT,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                popen_kwargs["env"] = dict(os.environ, PYTHONIOENCODING="utf-8")
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **popen_kwargs)
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            sink.seek(0)
            raw_output = sink.read()
            try:
                output = raw_output.decode("utf-8")
            except UnicodeDecodeError:
                # Windows PowerShell and native programs can emit the OEM
                # code page even when their output is redirected to a file.
                encoding = "oem" if sys.platform == "win32" else "utf-8"
                output = raw_output.decode(encoding, "replace")
        elapsed = time.time() - started
        if timed_out:
            return (
                f"命令超时（{timeout}s，已结束整个进程树）：{command}\n"
                f"部分输出（尾部）：\n{_tail(output, self.max_chars)}"
            )
        return (
            f"命令：{command}\n退出码：{proc.returncode}  用时：{elapsed:.1f}s\n"
            f"输出（尾部）：\n{_tail(output, self.max_chars)}"
        )

    # -- stats -------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "workspace": str(self.workspace),
            "allow_web": self.allow_web,
            "allow_run": self.allow_run,
            "counts": dict(self._counts),
            "calls": len(self.log),
        }


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _query_terms(query: str) -> set[str]:
    """CJK bigrams + ASCII tokens, used to sanity-check search relevance."""
    terms: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        for index in range(len(run) - 1):
            terms.add(run[index : index + 2])
    for token in re.findall(r"[A-Za-z0-9]{2,}", query):
        terms.add(token.lower())
    return terms


def _looks_relevant(query: str, results: Sequence[Mapping[str, str]]) -> bool:
    """Reject engines that answer with unrelated pages (e.g. dictionary pages)."""
    terms = _query_terms(query)
    if not terms:
        return bool(results)
    for item in results:
        blob = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
        if any(term in blob for term in terms):
            return True
    return False


def _simplify_query(query: str) -> str:
    """Second-chance query: keep keywords, drop quotes/punctuation and long tails."""
    cleaned = re.sub(r'["“”‘’（）()【】\[\]、，。,.!！?？:：;；]', " ", query)
    tokens = [token for token in re.split(r"\s+", cleaned) if token]
    if not tokens:
        return query
    # Prefer the leading keywords; cap length so engines don't truncate.
    simplified = " ".join(tokens[:5])
    return simplified[:60] if simplified else query


def _normalise_url(href: str, base: str) -> str:
    href = html_lib.unescape(href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/") and base:
        return base.rstrip("/") + href
    if href.startswith(("http://", "https://")):
        return href
    return ""


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ddg_url(href: str) -> str:
    href = html_lib.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    return href


def _tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        return "…（前面已省略）\n" + text[-limit:]
    return text or "（无输出）"


def _summarise_args(args: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in args.items():
        text = str(value)
        summary[key] = text if len(text) <= 120 else text[:117] + "…"
    return summary
