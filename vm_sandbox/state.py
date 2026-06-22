"""Persistence of the sandbox state.

:class:`SandboxState` is the domain value object representing the recorded
sandbox VM.  :class:`StateRepository` is the persistence port (a Protocol) the
application layer depends on; :class:`JsonStateRepository` is the filesystem
adapter.  These three form the persistence concern and live together so the
port, its JSON adapter, and the value they exchange stay cohesive.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .provider import VMInstance
from .ssh_util import restrict_to_owner

logger = logging.getLogger(__name__)

STATE_FILENAME = "state.json"


@dataclass(frozen=True)
class SandboxState:
    """The persisted sandbox record (what lives in ``state.json``).

    A domain value object: the use-case layer (``VMLifecycleManager``) operates
    on this typed value rather than a raw ``dict`` or filesystem paths.
    """

    vm_id: str
    provider: str
    name: str
    region: str
    public_ip: str
    public_ipv6: str
    ssh_host: str
    ssh_user: str
    created_at: float
    last_used: float

    @classmethod
    def for_vm(cls, vm: VMInstance, ssh_user: str, now: float) -> SandboxState:
        return cls(
            vm_id=vm.id,
            provider=vm.provider,
            name=vm.name,
            region=vm.region,
            public_ip=vm.public_ip,
            public_ipv6=vm.public_ipv6,
            ssh_host=vm.preferred_ip(),
            ssh_user=ssh_user,
            created_at=now,
            last_used=now,
        )

    def touched(self, now: float) -> SandboxState:
        """Immutable update: return a copy with ``last_used`` advanced."""
        return replace(self, last_used=now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StateRepository(Protocol):
    """Persistence seam for :class:`SandboxState` (dependency inversion).

    The application layer (``VMLifecycleManager``) depends on this Protocol,
    not on any concrete filesystem implementation — so persistence can be
    swapped (e.g. an in-memory repo in tests) without touching the use-case.
    """

    @property
    def path(self) -> Path:
        """Filesystem path backing the state (for diagnostics / back-comat)."""
        ...

    @property
    def key_dir(self) -> Path:
        """Directory holding the state file and SSH keypair."""
        ...

    def load(self) -> SandboxState | None:
        """Load the recorded state, or None if absent / unreadable."""
        ...

    def save(self, state: SandboxState) -> None:
        """Persist (overwrite) the recorded state."""
        ...

    def clear(self) -> None:
        """Delete the recorded state (idempotent)."""
        ...


class JsonStateRepository:
    """Filesystem-backed :class:`StateRepository`.

    Stores the sandbox state as JSON at ``<state_dir>/state.json`` and
    restricts it to the owner.  The SSH keypair lives alongside it in
    ``<state_dir>``.
    """

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / STATE_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    @property
    def key_dir(self) -> Path:
        return self._dir

    def load(self) -> SandboxState | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return self._state_from_dict(data)
        except (ValueError, OSError) as exc:
            # ValueError covers JSONDecodeError + UnicodeDecodeError; OSError
            # covers permission/IO failures.  Other exceptions propagate.
            logger.warning("vm-sandbox state file unreadable: %s", exc)
            return None

    @staticmethod
    def _state_from_dict(data: dict[str, Any]) -> SandboxState | None:
        known = set(SandboxState.__dataclass_fields__)
        kwargs = {k: v for k, v in data.items() if k in known}
        try:
            return SandboxState(**kwargs)
        except TypeError:
            # Missing required field — treat as no usable state.
            return None

    def save(self, state: SandboxState) -> None:
        self._path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        restrict_to_owner(str(self._path))  # connection details — owner-only

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
