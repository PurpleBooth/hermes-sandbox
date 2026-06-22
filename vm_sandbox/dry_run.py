"""Dry-run provider for the vm-sandbox plugin.

:class:`DryRunProvider` simulates provisioning entirely in memory — no cloud
credentials, no network.  It goes through the exact same lifecycle
(provision → get → list → teardown) as a real provider, so it is used to try
the plugin, exercise the lifecycle in tests, and verify a setup end-to-end
without spending money.

Registered as ``dry-run``; select it with ``VM_SANDBOX_PROVIDER=dry-run``.
"""

from __future__ import annotations

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
        vm_id = f"dry-run-{self._counter}"
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
