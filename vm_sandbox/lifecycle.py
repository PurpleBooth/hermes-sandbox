"""VM lifecycle manager for the vm-sandbox plugin.

Owns the on-disk state file that records the current sandbox VM so a VM
provisioned in a previous Hermes session is reused instead of creating a
new one each time.  Also owns the SSH keypair used to access the VM.

State file: ``$HERMES_HOME/vm-sandbox/state.json``

  {
    "vm_id": "...", "provider": "hetzner", "name": "hermes-sandbox",
    "region": "nbg1", "public_ip": "1.2.3.4", "ssh_user": "root",
    "created_at": 1719000000.0, "last_used": 1719000123.0
  }

The manager is deliberately provider-agnostic: it takes a
:class:`~vm_sandbox.provider.CloudProvider` instance and a config mapping.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .provider import CloudProvider, VMInstance
from .ssh_util import KeyPair, chmod_private_key, generate_keypair, restrict_to_owner

logger = logging.getLogger(__name__)

STATE_FILENAME = "state.json"
DEFAULT_INSTANCE_TYPE = "cx23"
DEFAULT_REGION = "nbg1"
DEFAULT_IMAGE = "ubuntu-24.04"
DEFAULT_SSH_USER = "root"
LABEL_KEY = "hermes-vm-sandbox"
LABEL_VALUE = "true"


@dataclass
class SandboxConfig:
    """Resolved configuration for the vm-sandbox plugin."""

    provider_name: str = "hetzner"
    instance_type: str = DEFAULT_INSTANCE_TYPE
    region: str = DEFAULT_REGION
    image: str = DEFAULT_IMAGE
    ssh_user: str = DEFAULT_SSH_USER
    token: str = ""
    vm_name: str = "hermes-sandbox"
    # 0 = never auto-teardown on session end; >0 = teardown after N idle seconds.
    auto_teardown_seconds: int = 0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> SandboxConfig:
        def _get(key: str, default: str = "") -> str:
            return env.get(key, default)

        provider_name = _get("VM_SANDBOX_PROVIDER", "hetzner")
        token = _resolve_token(provider_name, env)
        defaults = _provider_defaults(provider_name)

        return cls(
            provider_name=provider_name,
            instance_type=_get("VM_SANDBOX_INSTANCE_TYPE", defaults["instance_type"]),
            region=_get("VM_SANDBOX_REGION", defaults["region"]),
            image=_get("VM_SANDBOX_IMAGE", defaults["image"]),
            ssh_user=_get("VM_SANDBOX_SSH_USER", DEFAULT_SSH_USER),
            token=token,
            vm_name=_get("VM_SANDBOX_NAME", "hermes-sandbox"),
            auto_teardown_seconds=int(_get("VM_SANDBOX_AUTO_TEARDOWN_SECONDS", "0") or "0"),
        )

    def is_configured(self) -> bool:
        # A token is only required for real providers; fakes don't need one.
        return bool(self.token) or self.provider_name == "fake"


@dataclass(frozen=True)
class SandboxStatus:
    """A snapshot of the sandbox VM: persisted state plus a live probe.

    The persisted fields mirror the state file; the ``live_*`` fields are the
    result of a best-effort provider ``get()``.  ``to_dict()`` preserves the
    state-file key names (``vm_id``, ``provider``, ``public_ip``, …) so the
    slash/CLI ``status`` command emits stable JSON the specs can grep.
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
    live_status: str = ""
    live_ip: str = ""
    alive: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _LiveProbe:
    """Result of a best-effort provider ``get()`` during ``status()``.

    A small immutable value so ``status()`` builds the final ``SandboxStatus``
    in one shot instead of mutating fields after construction.
    """

    status: str
    ip: str
    alive: bool
    error: str = ""


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


def _resolve_token(provider_name: str, env: dict[str, str]) -> str:
    """Resolve the API token by delegating to the provider class.

    Falls back to ``VM_SANDBOX_TOKEN`` if the provider isn't registered yet
    (e.g. during early config loading before provider modules are imported).
    """
    try:
        from .provider import get_provider_class
        return get_provider_class(provider_name).resolve_token(env)
    except KeyError:
        return env.get("VM_SANDBOX_TOKEN") or ""


def _provider_defaults(provider_name: str) -> dict[str, str]:
    """Return the provider's default region/image/instance-type.

    Defaults live on the provider class (Open/Closed) so adding a provider
    needs no edit here.  Falls back to the base ``CloudProvider`` defaults if
    the provider isn't registered yet.
    """
    from .provider import CloudProvider, get_provider_class
    try:
        cls = get_provider_class(provider_name)
    except KeyError:
        return {
            "region": CloudProvider.default_region,
            "image": CloudProvider.default_image,
            "instance_type": CloudProvider.default_instance_type,
        }
    return {
        "region": cls.default_region,
        "image": cls.default_image,
        "instance_type": cls.default_instance_type,
    }


class VMLifecycleManager:
    """Provision, reuse, and tear down the sandbox VM.

    The use-case (application) layer: orchestrates a :class:`CloudProvider`
    and a :class:`StateRepository` (persistence seam) plus an injectable
    clock/key-factory.  It performs no filesystem I/O directly — that lives in
    the repository — so it stays unit-testable with an in-memory repo.
    """

    def __init__(
        self,
        provider: CloudProvider,
        config: SandboxConfig,
        hermes_home: Path,
        key_factory: Callable[[Path, str], KeyPair] = generate_keypair,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        state_repo: StateRepository | None = None,
    ) -> None:
        self.provider = provider
        self.config = config
        self.hermes_home = Path(hermes_home)
        self._key_factory = key_factory
        self._clock = clock
        self._sleep = sleep
        self._repo: StateRepository = state_repo or JsonStateRepository(
            self.hermes_home / "vm-sandbox"
        )

    # -- back-comat accessors (tests / diagnostics) ------------------------

    @property
    def state_dir(self) -> Path:
        return self._repo.key_dir

    @property
    def state_path(self) -> Path:
        return self._repo.path

    # -- keypair -----------------------------------------------------------

    def keypair(self) -> KeyPair:
        kp = self._key_factory(self._repo.key_dir, "hermes-vm-sandbox")
        chmod_private_key(kp.private_path)
        return kp

    # -- ssh backend wiring ------------------------------------------------

    def ssh_env(self) -> dict[str, str] | None:
        """Return the TERMINAL_SSH_* env vars pointing Hermes' ssh backend at the VM.

        Returns None when there is no recorded state or no resolvable host, so
        callers can no-op cleanly.  Encapsulates state access so the plugin
        entrypoint does not reach into private members.
        """
        state = self._repo.load()
        if not state:
            return None
        host = state.ssh_host or state.public_ip
        if not host:
            return None
        key_path = str(self._repo.key_dir / "hermes-vm-sandbox")
        return {
            "TERMINAL_ENV": "ssh",
            "TERMINAL_SSH_HOST": host,
            "TERMINAL_SSH_USER": state.ssh_user or self.config.ssh_user,
            "TERMINAL_SSH_KEY": key_path,
        }

    # -- core operations ---------------------------------------------------

    def status(self) -> SandboxStatus | None:
        """Return the recorded state, refreshed against the provider if alive."""
        state = self._repo.load()
        if not state:
            return None
        probe = self._probe_vm(state.vm_id)
        return SandboxStatus(
            vm_id=state.vm_id,
            provider=state.provider,
            name=state.name,
            region=state.region,
            public_ip=state.public_ip,
            public_ipv6=state.public_ipv6,
            ssh_host=state.ssh_host,
            ssh_user=state.ssh_user,
            created_at=state.created_at,
            last_used=state.last_used,
            live_status=probe.status,
            live_ip=probe.ip,
            alive=probe.alive,
            error=probe.error,
        )

    def _probe_vm(self, vm_id: str) -> _LiveProbe:
        """Best-effort provider liveness check, returned as an immutable value.

        Returns a dead/unknown probe on any error rather than raising, so
        ``status()`` can report recorded state even when the provider is down.
        """
        try:
            vm = self.provider.get(vm_id)
        except Exception as exc:
            return _LiveProbe(status="unknown", ip="", alive=False, error=str(exc))
        return _LiveProbe(status=vm.status, ip=vm.public_ip, alive=vm.is_running())

    def ensure_vm(self, max_key_attempts: int = 1) -> VMInstance:
        """Return a running sandbox VM, reusing or provisioning as needed.

        1. If state exists and the recorded VM is still running, reuse it.
        2. Otherwise provision a new VM (uploading the SSH key) and record it.
        """
        state = self._repo.load()
        if state and state.vm_id:
            try:
                vm = self.provider.get(state.vm_id)
                if vm.is_running() and vm.preferred_ip():
                    logger.info(
                        "vm-sandbox: reusing existing VM %s (%s)",
                        vm.id,
                        vm.preferred_ip(),
                    )
                    self._repo.save(state.touched(self._clock()))
                    return vm
                logger.info(
                    "vm-sandbox: recorded VM %s not running (status=%s) — reprovisioning",
                    vm.id,
                    vm.status,
                )
            except Exception as exc:
                logger.info("vm-sandbox: recorded VM unreachable (%s) — reprovisioning", exc)
            self._repo.clear()

        # Provision fresh.
        kp = self.keypair()
        labels = {LABEL_KEY: LABEL_VALUE}
        vm = self.provider.provision(
            name=self.config.vm_name,
            instance_type=self.config.instance_type,
            region=self.config.region,
            image=self.config.image,
            ssh_public_key=kp.public_key,
            labels=labels,
        )
        self._repo.save(SandboxState.for_vm(vm, self.config.ssh_user, self._clock()))
        logger.info("vm-sandbox: provisioned VM %s (%s)", vm.id, vm.preferred_ip())
        return vm

    def teardown(self) -> bool:
        """Destroy the recorded VM and clear state. Returns True if a VM was torn down."""
        state = self._repo.load()
        if not state or not state.vm_id:
            return False
        try:
            self.provider.teardown(state.vm_id)
            logger.info("vm-sandbox: tore down VM %s", state.vm_id)
        except Exception as exc:
            logger.warning("vm-sandbox: teardown of %s failed: %s", state.vm_id, exc)
            # Still clear local state so we don't keep pointing at a dead VM.
        self._repo.clear()
        return True

    def maybe_auto_teardown(self) -> bool:
        """Tear down if auto_teardown_seconds is set. Called on session end."""
        if self.config.auto_teardown_seconds <= 0:
            return False
        return self.teardown()
