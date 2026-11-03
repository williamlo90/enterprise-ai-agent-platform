"""Application-local readiness state; no global mutable process state."""

from dataclasses import dataclass


@dataclass
class RuntimeReadiness:
    ready: bool = False

    def is_ready(self) -> bool:
        return self.ready
