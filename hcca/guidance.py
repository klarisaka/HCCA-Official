"""Durable human messages, delivered once per active agent at its next turn."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


class GuidanceQueue:
    def __init__(self, run_dir: Path | str):
        self.directory = Path(run_dir) / "guidance"

    def publish(self, text: str, recipients: list[str], message_id: str = "") -> dict:
        identifier = message_id or uuid4().hex
        path = self.directory / (_key(identifier) + ".json")
        with _LOCK:
            if path.exists():
                message = json.loads(path.read_text(encoding="utf-8"))
                if message["message"] != text or message["recipient_agent_ids"] != list(dict.fromkeys(recipients)):
                    raise ValueError("message_id already belongs to different guidance")
            else:
                message = {"guidance_id": identifier, "message": text, "queued_at": _now(), "sequence": time.time_ns(),
                           "recipient_agent_ids": list(dict.fromkeys(recipients))}
                _write(path, message)
            return self._status(message)

    def _messages(self) -> list[dict]:
        if not self.directory.exists():
            return []
        return sorted((json.loads(path.read_text(encoding="utf-8")) for path in self.directory.glob("*.json")),
                      key=lambda message: (message["queued_at"], message.get("sequence", 0), message["guidance_id"]))

    def _receipt(self, message_id: str, agent_id: str) -> Path:
        return self.directory / "delivered" / _key(message_id) / (_key(agent_id) + ".json")

    def take(self, agent_id: str) -> list[dict]:
        messages = []
        with _LOCK:
            for message in self._messages():
                if agent_id not in message["recipient_agent_ids"]:
                    continue
                receipt = self._receipt(message["guidance_id"], agent_id)
                if receipt.exists():
                    continue
                messages.append({"role": "user", "content": message["message"]})
                _write(receipt, {"agent_id": agent_id, "delivered_at": _now()})
        return messages

    def _status(self, message: dict) -> dict:
        receipts = []
        for agent_id in message["recipient_agent_ids"]:
            path = self._receipt(message["guidance_id"], agent_id)
            if path.exists():
                receipts.append(json.loads(path.read_text(encoding="utf-8")))
        delivered = [receipt["agent_id"] for receipt in receipts]
        return {**message, "status": "delivered" if delivered else "queued",
                "delivered_at": min((receipt["delivered_at"] for receipt in receipts), default=None),
                "delivered_agent_ids": delivered,
                "pending_agent_ids": [agent for agent in message["recipient_agent_ids"] if agent not in delivered]}

    def snapshot(self) -> list[dict]:
        with _LOCK:
            return [self._status(message) for message in self._messages()]
