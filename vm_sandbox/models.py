"""Core domain model + error for vm-sandbox.

:class:`VMInstance` is the provider-agnostic description of a (provisioning or
running) VM — a frozen value object.  :class:`ApiError` is the structured REST
failure raised by the provider HTTP layer.  Both are pure domain types with no
dependency on any other vm_sandbox module, so everything else can import them
without risk of cycles.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class ApiError(RuntimeError):
    """A structured REST API failure.

    Raised by REST providers' ``_request`` when the cloud API returns a non-2xx
    status.  Carries the HTTP status code so callers (e.g. teardown idempotency)
    can branch on it without parsing the error message string.  Subclasses
    :class:`RuntimeError` so existing ``except RuntimeError`` sites still catch
    it.
    """

    def __init__(self, method: str, path: str, status_code: int, detail: str = "") -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        super().__init__(
            f"{method} {path} failed: HTTP {status_code}"
            + (f" {detail}" if detail else "")
        )


@dataclass(frozen=True)
class VMInstance:
    """A provider-agnostic description of a running (or provisioning) VM."""

    id: str
    name: str
    status: str  # provider-native status string; normalised via is_running()
    public_ip: str
    region: str
    provider: str
    public_ipv6: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # Statuses that mean "reachable over SSH".  "running" (Hetzner) and
    # "active" (DigitalOcean) are the two provider-native "up" states.
    _RUNNING_STATUSES = frozenset({"running", "active"})

    def is_running(self) -> bool:
        return self.status.lower() in self._RUNNING_STATUSES

    def preferred_ip(self) -> str:
        """Return the SSH target address — IPv6 preferred, IPv4 fallback."""
        return self.public_ipv6 or self.public_ip

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VMInstance:
        """Reconstruct a VMInstance from a ``to_dict()`` payload.

        Unknown keys are ignored (defensive against extra fields in the dict),
        not folded into ``extra`` — ``extra`` is restored only from the
        ``extra`` key.
        """
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in data.items() if k in known and k != "extra"}
        extra = data.get("extra") or {}
        return cls(extra=extra, **kwargs)
