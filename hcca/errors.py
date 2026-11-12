"""Exception hierarchy for HCCA."""

from __future__ import annotations


class HCCAError(Exception):
    """Base class for every error raised by the prototype."""


class BudgetOverflowError(HCCAError):
    """Raised when a mandatory context cannot fit into an agent budget."""

    def __init__(self, message: str, *, report: dict | None = None) -> None:
        super().__init__(message)
        self.report = report or {}


class ContractError(HCCAError):
    """Raised when a task contract is malformed or a result violates it."""


class ArtifactError(HCCAError):
    """Base error for artifact store problems."""


class ArtifactNotFound(ArtifactError):
    """Requested artifact / version does not exist."""


class RefParseError(ArtifactError):
    """A reference string could not be parsed."""


class RetrievalError(HCCAError):
    """Raised when a retrieval strategy fails in an unrecoverable way."""


class ToolError(HCCAError):
    """Raised when a sandboxed tool invocation fails."""


class ModelError(HCCAError):
    """Raised when the model backend returns something unusable."""


__all__ = [
    "ArtifactError",
    "ArtifactNotFound",
    "BudgetOverflowError",
    "ContractError",
    "HCCAError",
    "ModelError",
    "RefParseError",
    "RetrievalError",
    "ToolError",
]
