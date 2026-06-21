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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .provider import CloudProvider, VMInstance
from .ssh_util import KeyPair, chmod_private_key, generate_keypair

logger = logging.getLogger(__name__)

STATE_FILENAME = "state.json"
DEFAULT_INSTANCE_TYPE = "cx22"
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
        token = _resolve_provider_token(provider_name, env)

        # DO defaults differ from Hetzner.
        if provider_name == "digitalocean":
            default_region = "fra1"
            default_image = "ubuntu-24-04-x64"
            default_size = "s-2vcpu-4gb"
        else:
            default_region = DEFAULT_REGION
            default_image = DEFAULT_IMAGE
            default_size = DEFAULT_INSTANCE_TYPE

        return cls(
            provider_name=provider_name,
            instance_type=_get("VM_SANDBOX_INSTANCE_TYPE", default_size),
            region=_get("VM_SANDBOX_REGION", default_region),
            image=_get("VM_SANDBOX_IMAGE", default_image),
            ssh_user=_get("VM_SANDBOX_SSH_USER", DEFAULT_SSH_USER),
            token=token,
            vm_name=_get("VM_SANDBOX_NAME", "hermes-sandbox"),
            auto_teardown_seconds=int(_get("VM_SANDBOX_AUTO_TEARDOWN_SECONDS", "0") or "0"),
        )

    def is_configured(self) -> bool:
        # A token is only required for real providers; fakes don't need one.
        return bool(self.token) or self.provider_name == "fake"


def _resolve_provider_token(provider_name: str, env: dict[str, str]) -> str:
    """Pick the right cloud token env var for the configured provider."""
    if provider_name == "hetzner":
        return env.get("HCLOUD_TOKEN") or env.get("VM_SANDBOX_TOKEN") or ""
    if provider_name == "digitalocean":
        return (
            env.get("DIGITALOCEAN_TOKEN")
            or env.get("DO_API_TOKEN")
            or env.get("VM_SANDBOX_TOKEN")
            or ""
        )
    return env.get("VM_SANDBOX_TOKEN") or ""


class VMLifecycleManager:
    """Provision, reuse, and tear down the sandbox VM."""

    def __init__(
        self,
        provider: CloudProvider,
        config: SandboxConfig,
        hermes_home: Path,
        key_factory: Callable[[Path, str], KeyPair] = generate_keypair,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.config = config
        self.hermes_home = Path(hermes_home)
        self._key_factory = key_factory
        self._clock = clock
        self._sleep = sleep

        self.state_dir = self.hermes_home / "vm-sandbox"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / STATE_FILENAME

    # -- keypair -----------------------------------------------------------

    def keypair(self) -> KeyPair:
        kp = self._key_factory(self.state_dir, "hermes-vm-sandbox")
        chmod_private_key(kp.private_path)
        return kp

    # -- state file --------------------------------------------------------

    def _load_state(self) -> dict[str, Any] | None:
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("vm-sandbox state file unreadable: %s", exc)
            return None

    def _save_state(self, vm: VMInstance) -> None:
        now = self._clock()
        data = {
            "vm_id": vm.id,
            "provider": vm.provider,
            "name": vm.name,
            "region": vm.region,
            "public_ip": vm.public_ip,
            "public_ipv6": vm.public_ipv6,
            "ssh_host": vm.preferred_ip(),
            "ssh_user": self.config.ssh_user,
            "created_at": now,
            "last_used": now,
        }
        self.state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        chmod_private_key(str(self.state_path))  # no-op ownership, but keep 0600

    def _clear_state(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.state_path.unlink()

    # -- core operations ---------------------------------------------------

    def status(self) -> dict[str, Any] | None:
        """Return the recorded state, refreshed against the provider if alive."""
        state = self._load_state()
        if not state:
            return None
        # Best-effort liveness check; fall back to recorded state on error.
        try:
            vm = self.provider.get(state["vm_id"])
            state["live_status"] = vm.status
            state["live_ip"] = vm.public_ip
            state["alive"] = vm.is_running()
        except Exception as exc:
            state["live_status"] = "unknown"
            state["alive"] = False
            state["error"] = str(exc)
        return state

    def ensure_vm(self, max_key_attempts: int = 1) -> VMInstance:
        """Return a running sandbox VM, reusing or provisioning as needed.

        1. If state file exists and the recorded VM is still running, reuse it.
        2. Otherwise provision a new VM (uploading the SSH key) and record it.
        """
        state = self._load_state()
        if state and state.get("vm_id"):
            try:
                vm = self.provider.get(state["vm_id"])
                if vm.is_running() and vm.preferred_ip():
                    logger.info(
                        "vm-sandbox: reusing existing VM %s (%s)",
                        vm.id, vm.preferred_ip(),
                    )
                    # Touch last_used.
                    state["last_used"] = self._clock()
                    self.state_path.write_text(
                        json.dumps(state, indent=2), encoding="utf-8"
                    )
                    return vm
                logger.info(
                    "vm-sandbox: recorded VM %s not running (status=%s) — reprovisioning",
                    vm.id, vm.status,
                )
            except Exception as exc:
                logger.info("vm-sandbox: recorded VM unreachable (%s) — reprovisioning", exc)
            self._clear_state()

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
        self._save_state(vm)
        logger.info("vm-sandbox: provisioned VM %s (%s)", vm.id, vm.preferred_ip())
        return vm

    def teardown(self) -> bool:
        """Destroy the recorded VM and clear state. Returns True if a VM was torn down."""
        state = self._load_state()
        if not state or not state.get("vm_id"):
            return False
        vm_id = state["vm_id"]
        try:
            self.provider.teardown(vm_id)
            logger.info("vm-sandbox: tore down VM %s", vm_id)
        except Exception as exc:
            logger.warning("vm-sandbox: teardown of %s failed: %s", vm_id, exc)
            # Still clear local state so we don't keep pointing at a dead VM.
        self._clear_state()
        return True

    def maybe_auto_teardown(self) -> bool:
        """Tear down if auto_teardown_seconds is set. Called on session end."""
        if self.config.auto_teardown_seconds <= 0:
            return False
        return self.teardown()
