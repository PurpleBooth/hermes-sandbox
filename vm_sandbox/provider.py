"""Cloud provider abstraction for the vm-sandbox plugin.

Each provider implements the same :class:`CloudProvider` interface so the
lifecycle manager and session hook stay cloud-agnostic.  A provider is
registered via :func:`register_provider` and instantiated by name through
:func:`get_provider`.

The interface is deliberately minimal: provision a VM (injecting an SSH
public key), describe it, tear it down, and list VMs carrying a given
label so the lifecycle manager can find a previously-created sandbox.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

# Signature of the injectable HTTP request function used by REST providers.
# (method, path, token, body, timeout) -> parsed JSON dict.
RequestFn = Callable[..., dict[str, Any]]


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


class CloudProvider(ABC):
    """Interface every cloud backend must implement."""

    #: Registry key / display name.  Subclasses set this.
    name: str = ""

    #: Environment variables checked (in order) when resolving the API token.
    #: The first non-empty value wins.  ``VM_SANDBOX_TOKEN`` is a generic
    #: fallback so users can set one token for any provider.
    token_env_keys: tuple[str, ...] = ("VM_SANDBOX_TOKEN",)

    #: Provider-specific defaults applied by ``SandboxConfig.from_env`` when the
    #: corresponding ``VM_SANDBOX_*`` env var is unset.  Subclasses override
    #: these so adding a provider needs no edit to ``SandboxConfig``.
    default_region: str = "nbg1"
    default_image: str = "ubuntu-24.04"
    default_instance_type: str = "cx23"

    @classmethod
    def resolve_token(cls, env: dict[str, str]) -> str:
        """Return the first non-empty token from :attr:`token_env_keys`."""
        for key in cls.token_env_keys:
            val = env.get(key, "")
            if val:
                return val
        return ""

    @abstractmethod
    def provision(
        self,
        name: str,
        instance_type: str,
        region: str,
        image: str,
        ssh_public_key: str,
        labels: dict[str, str] | None = None,
    ) -> VMInstance:
        """Create (or reuse) a VM and return its description.

        Implementations should block until the VM has a public IP, but may
        return before SSH is fully reachable — the lifecycle manager polls
        readiness separately.
        """

    @abstractmethod
    def get(self, vm_id: str) -> VMInstance:
        """Return the current description of a VM by provider id."""

    @abstractmethod
    def teardown(self, vm_id: str) -> None:
        """Destroy the VM.  Idempotent: a missing VM is a success."""

    @abstractmethod
    def list_by_label(self, label_key: str, label_value: str) -> list[VMInstance]:
        """Return VMs carrying ``label_key=label_value``."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[CloudProvider]] = {}


def register_provider(cls: type[CloudProvider]) -> type[CloudProvider]:
    """Class decorator: register a :class:`CloudProvider` subclass by ``name``."""
    if not getattr(cls, "name", ""):
        raise ValueError(f"{cls.__name__} must set a non-empty `name`")
    _REGISTRY[cls.name] = cls
    return cls


def get_provider(name: str, **kwargs: Any) -> CloudProvider:
    """Instantiate a registered provider by name."""
    return get_provider_class(name)(**kwargs)


def get_provider_class(name: str) -> type[CloudProvider]:
    """Return the registered provider class by name (without instantiating)."""
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(
            f"Unknown cloud provider {name!r}. Registered: {sorted(_REGISTRY)}"
        ) from exc


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------- #
# Functional helpers
# --------------------------------------------------------------------------- #


def poll_until(
    predicate: Callable[[], bool],
    *,
    interval: float,
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Call ``predicate`` until it returns True, sleeping ``interval`` between tries.

    Raises ``TimeoutError`` if ``predicate`` is still falsy after ``timeout``
    seconds.  ``sleep`` and ``clock`` are injectable so the poll loop can be
    exercised in tests without real waiting.  The predicate is always called at
    least once before sleeping.
    """
    deadline = clock() + timeout
    while True:
        if predicate():
            return
        if clock() >= deadline:
            raise TimeoutError(f"poll_until: condition not met within {timeout}s")
        sleep(interval)


# ---------------------------------------------------------------------------
# A fake provider used by tests and as a structural reference.
# ---------------------------------------------------------------------------


@register_provider
class FakeProvider(CloudProvider):
    """In-memory provider for tests.  Not registered for production use."""

    name = "fake"

    def __init__(self, token: str = "", provision_delay: float = 0.0) -> None:
        # ``token`` is accepted (and ignored) so ``get_provider(name, token=…)``
        # works uniformly across real and fake providers.
        self._vms: dict[str, VMInstance] = {}
        self._counter = 0
        self.calls: list[dict[str, Any]] = []
        self.provision_delay = provision_delay

    def provision(
        self,
        name: str,
        instance_type: str,
        region: str,
        image: str,
        ssh_public_key: str,
        labels: dict[str, str] | None = None,
    ) -> VMInstance:
        self._counter += 1
        vm_id = f"fake-{self._counter}"
        vm = VMInstance(
            id=vm_id,
            name=name,
            status="running",
            public_ip=f"10.0.0.{self._counter}",
            region=region,
            provider=self.name,
            extra={"labels": labels or {}, "instance_type": instance_type},
        )
        self._vms[vm_id] = vm
        self.calls.append({"op": "provision", "vm_id": vm_id, "name": name})
        return vm

    def get(self, vm_id: str) -> VMInstance:
        if vm_id not in self._vms:
            raise KeyError(vm_id)
        return self._vms[vm_id]

    def teardown(self, vm_id: str) -> None:
        self._vms.pop(vm_id, None)
        self.calls.append({"op": "teardown", "vm_id": vm_id})

    def list_by_label(self, label_key: str, label_value: str) -> list[VMInstance]:
        return [
            vm
            for vm in self._vms.values()
            if vm.extra.get("labels", {}).get(label_key) == label_value
        ]
