#!/usr/bin/env python3
"""Install the local HCCA plugin and its preset into an existing DSH profile."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

START = "# hcca-managed:start"
END = "# hcca-managed:end"
OWNED_IDS = re.compile(r"^(\s*)-\s+id:\s*['\"]?(?:hcca-host|preset-hcca)['\"]?\s*(?:#.*)?$")


def remove_old_entries(text):
    text = re.sub(r"(?ms)^# hcca-managed:start\n.*?^# hcca-managed:end\s*\n?", "", text)
    lines = text.splitlines(keepends=True)
    kept = []
    index = 0
    while index < len(lines):
        match = OWNED_IDS.match(lines[index].rstrip("\r\n"))
        if not match:
            kept.append(lines[index])
            index += 1
            continue
        indent = len(match.group(1))
        index += 1
        while index < len(lines):
            line = lines[index]
            if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= indent:
                break
            index += 1
    # Removing the only plugin in an insert operation leaves an empty parent.
    return re.sub(r"(?m)^- insert:\s*\n(?=(?:\s*#.*\n|\s*\n)*(?:\S|\Z))", "", "".join(kept)).rstrip()


def managed_patch(plugin, root, python, port):
    quote = lambda value: json.dumps(str(value), ensure_ascii=False)
    host = (
        "- insert:\n"
        "    - id: hcca-host\n"
        "      name: '@hcca/dsh-hcca'\n"
        "      config:\n"
        f"        port: {port}\n"
        f"        python: {quote(python)}\n"
        f"        sidecarRoot: {quote(root)}\n"
    )
    preset = (plugin / "hcca-preset.patch.yml").read_text(encoding="utf-8")
    preset = preset.replace("port: !!js Number(process.env.HCCA_SIDECAR_PORT || 8765)", f"port: {port}")
    return f"{START}\n{host}\n{preset.rstrip()}\n{END}\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="web", help="DSH profile name, for example web or desktop")
    parser.add_argument("--dsh-home", type=Path, default=Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")))
    parser.add_argument("--python", default=sys.executable, help="Python 3.11+ used by the sidecar")
    parser.add_argument("--port", type=int, default=int(os.environ.get("HCCA_SIDECAR_PORT", "8765")))
    parser.add_argument("--dsh", default="dsh", help="DSH CLI executable")
    parser.add_argument("--dry-run", action="store_true", help="Print paths and planned changes")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer is required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.profile) or args.profile in (".", ".."):
        parser.error("Use a profile name such as web or desktop")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    plugin = Path(__file__).resolve().parent
    root = plugin.parents[1]
    if not (root / "hcca" / "dsh_worker.py").is_file():
        parser.error("Keep this plugin inside the complete HCCA repository")
    profile = args.dsh_home.expanduser().resolve() / "profiles" / args.profile
    print(f"HCCA plugin 0.2.1\nProfile {profile}\nSidecar {root}\nPort {args.port}")
    if args.dry_run:
        print("Will back up this profile, install the local plugin, and update its HCCA preset.")
        return
    dsh = shutil.which(args.dsh)
    if not dsh:
        parser.error("DSH CLI was not found. Add it to PATH or pass --dsh /path/to/dsh")
    subprocess.run([args.python, "-c", "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"], check=True)
    backup = profile / "hcca-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup.mkdir(parents=True)
    for filename in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "cordis.patch.yml", "hcca-tools.mjs", "hcca-guard.mjs"):
        source = profile / filename
        if source.exists():
            shutil.copy2(source, backup / filename)
    print(f"Profile backup {backup}")
    environment = {**os.environ, "DSH_HOME": str(args.dsh_home.expanduser().resolve())}
    subprocess.run([dsh, "plugin", "--profile", args.profile, "add", "--ignore-scripts", "file:" + plugin.as_posix()],
                   check=True, env=environment)
    patch_path = profile / "cordis.patch.yml"
    original = patch_path.read_text(encoding="utf-8-sig") if patch_path.exists() else ""
    patched = remove_old_entries(original) + "\n\n" + managed_patch(plugin, root, args.python, args.port)
    for source, target in (("preset-tools.mjs", "hcca-tools.mjs"), ("preset-filter.mjs", "hcca-guard.mjs")):
        shutil.copy2(plugin / source, profile / target)
    temporary = patch_path.with_suffix(".yml.hcca.tmp")
    temporary.write_text(patched.lstrip("\n"), encoding="utf-8")
    os.replace(temporary, patch_path)
    print(f"Installed. Restart DSH {args.profile}, then choose the HCCA preset in a conversation.")


if __name__ == "__main__":
    main()
