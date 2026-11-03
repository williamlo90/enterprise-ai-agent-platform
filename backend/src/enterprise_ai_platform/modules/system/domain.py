"""Transport-independent health values."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProbeResult:
    status: Literal["ok", "ready", "unavailable"]
    scope: Literal["process", "bootstrap"]
