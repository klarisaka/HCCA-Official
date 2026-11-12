"""Prior knowledge of the simulated model ("world model").

``ScriptedLLM`` is a deterministic stand-in for an LLM.  It does not "know"
the repository; it only knows general repair patterns (``issue_fixes``), the
canonical statements of the facts it is asked to recognise (``facts``) and
which observable evidence triggers which issue (``trigger_patterns``).  All of
that is supplied by the evaluation fixture, not by the control plane, so the
architecture under test never sees ground truth.

The model only applies this prior knowledge to evidence that is *visible in
its delivered context* — that is exactly the property HCCA is designed to
exploit, and the property the evaluation measures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, (list, set)):
        return tuple(value)
    return (value,)


@dataclass(frozen=True)
class FactSpec:
    """A ground-truth fact the system may discover and must preserve."""

    fact_id: str
    statement: str
    source_ref: str
    domain: str = "general"
    keywords: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "statement": self.statement,
            "source_ref": self.source_ref,
            "domain": self.domain,
            "keywords": list(self.keywords),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FactSpec":
        return cls(
            fact_id=str(data["fact_id"]),
            statement=str(data["statement"]),
            source_ref=str(data.get("source_ref", "")),
            domain=str(data.get("domain", "general")),
            keywords=tuple(str(k) for k in _as_tuple(data.get("keywords"))),
        )


@dataclass(frozen=True)
class IssueFix:
    """A known defect pattern plus the concrete repair the model can emit."""

    issue_code: str
    domain: str
    summary: str
    fact_ids: tuple[str, ...] = ()
    trigger_patterns: tuple[str, ...] = ()
    required_refs: tuple[str, ...] = ()
    patch: tuple[dict[str, Any], ...] = ()
    # v0.2: conclusions are conditional result caches, not flat summaries.
    requires: tuple[str, ...] = ()
    not_established: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_code": self.issue_code,
            "domain": self.domain,
            "summary": self.summary,
            "fact_ids": list(self.fact_ids),
            "trigger_patterns": list(self.trigger_patterns),
            "required_refs": list(self.required_refs),
            "patch": [dict(p) for p in self.patch],
            "requires": list(self.requires),
            "not_established": list(self.not_established),
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IssueFix":
        return cls(
            issue_code=str(data["issue_code"]),
            domain=str(data.get("domain", "general")),
            summary=str(data.get("summary", "")),
            fact_ids=tuple(str(f) for f in _as_tuple(data.get("fact_ids"))),
            trigger_patterns=tuple(str(p) for p in _as_tuple(data.get("trigger_patterns"))),
            required_refs=tuple(str(r) for r in _as_tuple(data.get("required_refs"))),
            patch=tuple(dict(p) for p in (data.get("patch") or [])),
            requires=tuple(str(r) for r in _as_tuple(data.get("requires"))),
            not_established=tuple(
                str(n) for n in _as_tuple(data.get("not_established"))
            ),
            depends_on=tuple(str(d) for d in _as_tuple(data.get("depends_on"))),
        )


@dataclass(frozen=True)
class EvidenceRule:
    """A generic evidence -> claim rule (used for facts without a full fix)."""

    rule_id: str
    contains: tuple[str, ...]
    claim: str
    fact_ids: tuple[str, ...] = ()
    domain: str = ""

    def matches(self, text: str) -> bool:
        lowered = text.lower()
        return any(token.lower() in lowered for token in self.contains)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "contains": list(self.contains),
            "claim": self.claim,
            "fact_ids": list(self.fact_ids),
            "domain": self.domain,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceRule":
        return cls(
            rule_id=str(data["rule_id"]),
            contains=tuple(str(c) for c in _as_tuple(data.get("contains"))),
            claim=str(data.get("claim", "")),
            fact_ids=tuple(str(f) for f in _as_tuple(data.get("fact_ids"))),
            domain=str(data.get("domain", "")),
        )


@dataclass
class ModelWorld:
    """Everything the scripted model may bring to bear, and nothing else."""

    facts: dict[str, FactSpec] = field(default_factory=dict)
    issue_fixes: dict[str, IssueFix] = field(default_factory=dict)
    evidence_rules: tuple[EvidenceRule, ...] = ()
    domain_keywords: dict[str, tuple[str, ...]] = field(default_factory=dict)
    faults: dict[str, Any] = field(default_factory=dict)

    def fixes_for_domain(self, domain: str) -> list[IssueFix]:
        return [fix for fix in self.issue_fixes.values() if fix.domain == domain]

    def fact_statement(self, fact_id: str) -> str:
        fact = self.facts.get(fact_id)
        return fact.statement if fact else f"<unknown fact {fact_id}>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "facts": {k: v.to_dict() for k, v in self.facts.items()},
            "issue_fixes": {k: v.to_dict() for k, v in self.issue_fixes.items()},
            "evidence_rules": [r.to_dict() for r in self.evidence_rules],
            "domain_keywords": {k: list(v) for k, v in self.domain_keywords.items()},
            "faults": dict(self.faults),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelWorld":
        return cls(
            facts={
                str(k): FactSpec.from_dict(v)
                for k, v in (data.get("facts") or {}).items()
            },
            issue_fixes={
                str(k): IssueFix.from_dict(v)
                for k, v in (data.get("issue_fixes") or {}).items()
            },
            evidence_rules=tuple(
                EvidenceRule.from_dict(r) for r in (data.get("evidence_rules") or [])
            ),
            domain_keywords={
                str(k): tuple(str(x) for x in (v or []))
                for k, v in (data.get("domain_keywords") or {}).items()
            },
            faults=dict(data.get("faults") or {}),
        )

    @classmethod
    def load(cls, path: Path | str) -> "ModelWorld":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def save(self, path: Path | str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


__all__ = [
    "EvidenceRule",
    "FactSpec",
    "IssueFix",
    "ModelWorld",
]
