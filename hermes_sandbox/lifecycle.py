"""VM lifecycle use-case for the sandbox plugin.

:class:`VMLifecycleManager` is the application-layer orchestrator: it provisions,
reuses, and tears down the sandbox VM by coordinating a
:class:`~hermes_sandbox.provider.CloudProvider` with a
:class:`~hermes_sandbox.state.StateRepository` (persistence seam) and an injectable
clock/key-factory.  It performs no filesystem I/O directly — that lives in the
repository — so it stays unit-testable with an in-memory repo.

This module also holds the two read-models the manager produces:
:class:`SandboxStatus` (the public snapshot returned by ``status()``) and the
private :class:`_LiveProbe` (the best-effort liveness result folded into it).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import SandboxConfig
from .models import VMInstance
from .provider import CloudProvider
from .ssh_util import KeyPair, chmod_private_key, generate_keypair
from .state import JsonStateRepository, SandboxState, StateRepository

logger = logging.getLogger(__name__)

LABEL_KEY = "hermes-sandbox"
LABEL_VALUE = "true"


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
            self.hermes_home / "sandbox"
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
        kp = self._key_factory(self._repo.key_dir, "hermes-sandbox")
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
        key_path = str(self._repo.key_dir / "hermes-sandbox")
        return {
            "TERMINAL_ENV": "ssh",
            "TERMINAL_SSH_HOST": host,
            "TERMINAL_SSH_USER": state.ssh_user or self.config.ssh_user,
            "TERMINAL_SSH_KEY": key_path,
            **(
                {"TERMINAL_SSH_PORT": str(state.ssh_port)}
                if state.ssh_port != 22
                else {}
            ),
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

    def list_vms(self) -> list[VMInstance]:
        """List all sandbox VMs on the cloud provider (tagged hermes-sandbox)."""
        return self.provider.list_by_label(LABEL_KEY, LABEL_VALUE)

    def recreate(self) -> VMInstance:
        """Tear down the current VM (if any) and provision a fresh one.

        Useful when a VM is broken or unreachable: destroys the recorded VM,
        clears state, then provisions a new one in its place.
        """
        self.teardown()
        return self.ensure_vm()

    def ensure_vm(self) -> VMInstance:
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
                        "sandbox: reusing existing VM %s (%s)",
                        vm.id,
                        vm.preferred_ip(),
                    )
                    self._repo.save(state.touched(self._clock()))
                    return vm
                logger.info(
                    "sandbox: recorded VM %s not running (status=%s) — reprovisioning",
                    vm.id,
                    vm.status,
                )
            except Exception as exc:
                logger.info("sandbox: recorded VM unreachable (%s) — reprovisioning", exc)
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
        logger.info("sandbox: provisioned VM %s (%s)", vm.id, vm.preferred_ip())
        return vm

    def teardown(self) -> bool:
        """Destroy the recorded VM and clear state. Returns True if a VM was torn down."""
        state = self._repo.load()
        if not state or not state.vm_id:
            return False
        try:
            self.provider.teardown(state.vm_id)
            logger.info("sandbox: tore down VM %s", state.vm_id)
        except Exception as exc:
            logger.warning("sandbox: teardown of %s failed: %s", state.vm_id, exc)
            # Still clear local state so we don't keep pointing at a dead VM.
        self._repo.clear()
        return True

    def maybe_auto_teardown(self) -> bool:
        """Tear down if auto_teardown_seconds is set. Called on session end."""
        if self.config.auto_teardown_seconds <= 0:
            return False
        return self.teardown()
