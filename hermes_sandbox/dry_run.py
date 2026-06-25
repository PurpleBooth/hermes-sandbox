"""Dry-run provider for the sandbox plugin.

:class:`DryRunProvider` simulates provisioning entirely in memory — no cloud
credentials, no network.  It goes through the exact same lifecycle
(provision → get → list → teardown) as a real provider, so it is used to try
the plugin, exercise the lifecycle in tests, and verify a setup end-to-end
without spending money.

Each provisioned VM gets a random UUID (like a real cloud provider would
assign) and a random private IP, so multiple independent provider instances
— as used in isolated session mode — produce distinct VMs.

Registered as ``dry-run``; select it with ``SANDBOX_PROVIDER=dry-run``.
"""

from __future__ import annotations

import random
import uuid
from typing import Any

from .models import VMInstance
from .provider import CloudProvider, register_provider


@register_provider
class DryRunProvider(CloudProvider):
    """In-memory provider that never touches a cloud."""

    name = "dry-run"

    def __init__(self, token: str = "", provision_delay: float = 0.0) -> None:
        # ``token`` is accepted (and ignored) so ``get_provider(name, token=…)``
        # works uniformly across real and dry-run providers.
        self._vms: dict[str, VMInstance] = {}
        self.calls: list[dict[str, Any]] = []
        self.provision_delay = provision_delay

    @staticmethod
    def _random_ip() -> str:
        """Return a random private IPv4 in 10.0.0.0/8."""
        return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"

    def provision(
        self,
        name: str,
        instance_type: str,
        region: str,
        image: str,
        ssh_public_key: str,
        labels: dict[str, str] | None = None,
        forward_env: dict[str, str] | None = None,
    ) -> VMInstance:
        vm_id = str(uuid.uuid4())
        vm = VMInstance(
            id=vm_id,
            name=name,
            status="running",
            public_ip=self._random_ip(),
            region=region,
            provider=self.name,
            extra={
                "labels": labels or {},
                "instance_type": instance_type,
                "forward_env": forward_env or {},
            },
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
