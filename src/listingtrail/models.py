from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Record:
    key: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchContext:
    """Mutable per-record context handed to each pipeline stage.

    The context is deliberately transport-only: it carries the batch identity,
    the current source position, the fetch session and the previously stored
    listing so that individual stages can stay focused on their own decision.
    """

    batch_id: str | None = None
    position: int = 0
    session: Any = None
    policy_set: Any = None
    prior: dict[str, Any] | None = None
    now: Callable[[], float] | None = None
