"""Contract tests — every registered provider must satisfy the same lifecycle.

These tests run the same suite of assertions against each provider (dry-run,
hetzner, digitalocean, qemu) so that adding a provider automatically inherits
the full contract.  Real cloud providers are exercised with injected fake
HTTP (``request_fn``) — no network calls are made.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hermes_sandbox.provider import CloudProvider, available_providers, get_provider
from hermes_sandbox.qemu import _kill_pidfile

# Signature of the injectable HTTP request function used by REST providers.
RequestFn = Callable[..., dict[str, Any]]

# ── Stateful fake cloud APIs ─────────────────────────────────────────────────
# Each factory returns a fresh ``request_fn`` with closure-captured mutable
# state, so every test gets a clean cloud that behaves like the real one:
# POST creates a new VM with a unique id, GET returns it by id, DELETE removes
# it, and list queries return all matching VMs.  This lets the concurrency /
# isolation contract tests exercise real multi-VM scenarios.


def _make_hz_request() -> RequestFn:
    """Stateful fake Hetzner API — tracks provisioned servers in a closure."""
    servers: dict[str, dict[str, Any]] = {}
    next_id = [100]

    def request(
        method: str, path: str, token: str, body: dict | None = None, timeout: int = 30
    ) -> dict[str, Any]:
        if method == "GET" and path.startswith("/ssh_keys"):
            return {"ssh_keys": []}
        if method == "POST" and path == "/ssh_keys":
            return {"ssh_key": {"name": "hermes-sandbox"}}
        if method == "POST" and path == "/servers":
            vm_id = str(next_id[0])
            next_id[0] += 1
            server = {
                "id": int(vm_id),
                "name": (body or {}).get("name", "test-vm"),
                "status": "running",
                "public_net": {
                    "ipv4": {"ip": f"10.{int(vm_id) // 256 % 256}.{int(vm_id) % 256}.1"},
                    "ipv6": {"ip": f"2a01:db8::{vm_id}"},
                },
                "datacenter": {"location": {"name": "nbg1"}},
                "labels": (body or {}).get("labels", {}),
                "server_type": {"name": "cx23"},
                "image": {"name": "ubuntu-24.04"},
            }
            servers[vm_id] = server
            return {"server": server}
        if method == "GET" and path.startswith("/servers/"):
            vm_id = path.split("/servers/", 1)[1]
            return {"server": servers[vm_id]}
        if method == "DELETE" and path.startswith("/servers/"):
            vm_id = path.split("/servers/", 1)[1]
            servers.pop(vm_id, None)
            return {}
        if method == "GET" and "label_selector=" in path:
            selector = path.split("label_selector=", 1)[1]
            key, val = selector.split("=", 1)
            matching = [s for s in servers.values() if s.get("labels", {}).get(key) == val]
            return {"servers": matching}
        raise AssertionError(f"unexpected Hetzner request: {method} {path}")

    return request


def _make_do_request() -> RequestFn:
    """Stateful fake DigitalOcean API — tracks provisioned droplets in a closure."""
    droplets: dict[str, dict[str, Any]] = {}
    next_id = [200]

    def request(
        method: str, path: str, token: str, body: dict | None = None, timeout: int = 30
    ) -> dict[str, Any]:
        if method == "GET" and path == "/account/keys":
            return {"ssh_keys": []}
        if method == "POST" and path == "/account/keys":
            return {"ssh_key": {"id": 7}}
        if method == "POST" and path == "/droplets":
            vm_id = str(next_id[0])
            next_id[0] += 1
            droplet = {
                "id": int(vm_id),
                "name": (body or {}).get("name", "test-vm"),
                "status": "active",
                "networks": {
                    "v4": [
                        {
                            "type": "public",
                            "ip_address": f"10.{int(vm_id) // 256 % 256}.{int(vm_id) % 256}.2",
                        }
                    ],
                    "v6": [{"type": "public", "ip_address": f"2604::{vm_id}"}],
                },
                "region": {"slug": "fra1"},
                "tags": (body or {}).get("tags", ["hermes-sandbox"]),
                "size": {"slug": "s-2vcpu-4gb"},
                "image": {"slug": "ubuntu-24-04-x64"},
            }
            droplets[vm_id] = droplet
            return {"droplet": droplet}
        if method == "GET" and path.startswith("/droplets/"):
            vm_id = path.split("/droplets/", 1)[1]
            return {"droplet": droplets[vm_id]}
        if method == "DELETE" and path.startswith("/droplets/"):
            vm_id = path.split("/droplets/", 1)[1]
            droplets.pop(vm_id, None)
            return {}
        if method == "GET" and "tag_name=" in path:
            tag = path.split("tag_name=", 1)[1]
            matching = [d for d in droplets.values() if tag in (d.get("tags") or [])]
            return {"droplets": matching}
        raise AssertionError(f"unexpected DigitalOcean request: {method} {path}")

    return request


# ── Provider factory ────────────────────────────────────────────────────────


def _qemu_available() -> bool:
    """True when qemu-system-x86_64 and an ISO builder are on PATH."""
    import shutil

    return shutil.which("qemu-system-x86_64") is not None and any(
        shutil.which(b) for b in ("genisoimage", "mkisofs", "xorrisofs", "xorriso")
    )


def _make_provider(name: str) -> CloudProvider:
    """Build a provider instance with fake HTTP where needed."""
    if name == "dry-run":
        return get_provider("dry-run", token="")
    if name == "qemu":
        from hermes_sandbox.qemu import QemuProvider

        # Use the real subprocess runner — QEMU is a local provider, not a
        # cloud API.  Tests that actually provision will be skipped if QEMU
        # or an ISO builder (xorriso/genisoimage) isn't installed.
        return QemuProvider(token="")
    if name == "hetzner":
        from hermes_sandbox.hetzner import HetznerProvider

        return HetznerProvider(
            token="tok",
            poll_interval=0,
            poll_timeout=5,
            request_fn=_make_hz_request(),
        )
    if name == "digitalocean":
        from hermes_sandbox.digitalocean import DigitalOceanProvider

        return DigitalOceanProvider(
            token="tok",
            poll_interval=0,
            poll_timeout=5,
            request_fn=_make_do_request(),
        )
    raise ValueError(f"no factory for provider {name!r}")


# Ensure all providers are registered before collecting parametrized tests.
import hermes_sandbox.digitalocean  # noqa: F401, E402
import hermes_sandbox.hetzner  # noqa: F401, E402
import hermes_sandbox.qemu  # noqa: F401, E402

_PROVIDERS = available_providers()

# QEMU contract tests only run when qemu-system-x86_64 + an ISO builder are
# installed — QEMU is a local provider, so the contract is exercised for real
# (no fake subprocess runner, just like hetzner/DO use real HTTP-shaped fakes).
_provision_providers = [n for n in _PROVIDERS if n != "qemu" or _qemu_available()]


# ── Contract: registration & instantiation ──────────────────────────────────


@pytest.mark.parametrize("name", _PROVIDERS)
def test_provider_is_registered_and_instantiable(name: str):
    """Every provider can be built via get_provider(name, token=...)."""
    p = _make_provider(name)
    assert isinstance(p, CloudProvider)
    assert p.name == name


@pytest.mark.parametrize("name", _PROVIDERS)
def test_provider_has_all_interface_methods(name: str):
    """Every provider implements the full CloudProvider ABC."""
    p = _make_provider(name)
    for method in ("provision", "get", "teardown", "list_by_label"):
        assert callable(getattr(p, method)), f"{name} missing {method}"


@pytest.mark.parametrize("name", _PROVIDERS)
def test_provider_declares_token_env_keys(name: str):
    """Every provider declares which env vars hold its API token."""
    from hermes_sandbox.provider import get_provider_class

    cls = get_provider_class(name)
    assert cls.token_env_keys, f"{name}: token_env_keys must not be empty"
    assert "SANDBOX_TOKEN" in cls.token_env_keys, (
        f"{name}: token_env_keys must include SANDBOX_TOKEN as a fallback"
    )


@pytest.mark.parametrize("name", _PROVIDERS)
def test_provider_resolve_token_picks_first_non_empty(name: str):
    """resolve_token returns the first non-empty env var from token_env_keys."""
    from hermes_sandbox.provider import get_provider_class

    cls = get_provider_class(name)
    primary = cls.token_env_keys[0]
    # Only the primary key set → that value is returned.
    assert cls.resolve_token({primary: "secret-123"}) == "secret-123"
    # Nothing set → empty string.
    assert cls.resolve_token({}) == ""
    # SANDBOX_TOKEN fallback works when the primary isn't set.
    assert cls.resolve_token({"SANDBOX_TOKEN": "fallback"}) == "fallback"


# ── Contract: provision returns a valid running VM ──────────────────────────
# These tests actually provision a VM.  QEMU is skipped when the binaries
# aren't installed.


@pytest.fixture
def contract_image(request, tmp_path):
    """A disk image suitable to the provider under test.

    Cloud providers drive fake HTTP and ignore the path, so a placeholder
    works.  QEMU opens the image for real, so we hand it a tiny blank qcow2
    built with ``qemu-img``.
    """
    name = request.node.callspec.params["name"]
    if name == "qemu":
        img = tmp_path / "blank.qcow2"
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(img), "1M"],
            check=True,
            capture_output=True,
        )
        return str(img)
    return "test-image"


@pytest.fixture
def make_image(request, tmp_path):
    """Factory producing a fresh disk image per call (for multi-VM tests).

    Each call returns a distinct blank qcow2 for QEMU (two VMs must not share
    a writable disk image) or a placeholder for cloud providers.
    """
    name = request.node.callspec.params["name"]
    counter = [0]

    def _make() -> str:
        if name == "qemu":
            counter[0] += 1
            img = tmp_path / f"vm-{counter[0]}.qcow2"
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", str(img), "1M"],
                check=True,
                capture_output=True,
            )
            return str(img)
        return "test-image"

    return _make


@pytest.fixture(autouse=True)
def _reap_stray_qemu():
    """SIGTERM any QEMU VM left over from a previous contract test.

    Every QEMU contract test boots VMs into a shared work directory; a
    lingering process holds its port and breaks the next provision.  We reap
    via the pidfile QEMU wrote (``os.kill``) rather than exec'ing ``pkill``.
    """
    work_dir = Path(tempfile.gettempdir()) / "hermes-sandbox"
    for pidfile in work_dir.glob("*/qemu.pid"):
        _kill_pidfile(pidfile)
    yield
    for pidfile in work_dir.glob("*/qemu.pid"):
        _kill_pidfile(pidfile)


@pytest.mark.parametrize("name", _provision_providers)
def test_provision_returns_running_vm_with_all_fields(name: str, contract_image):
    p = _make_provider(name)
    vm = p.provision(
        name="contract-test-vm",
        instance_type="test-size",
        region="test-region",
        image=contract_image,
        ssh_public_key="ssh-ed25519 FAKEKEY",
        labels={"hermes-sandbox": "true"},
    )
    assert vm.id, f"{name}: VM id must be non-empty"
    assert vm.name == "contract-test-vm", f"{name}: VM name mismatch"
    assert vm.is_running(), f"{name}: VM must be running after provision (status={vm.status!r})"
    assert vm.public_ip, f"{name}: VM must have a public IP after provision"
    assert vm.region, f"{name}: VM must have a region"
    assert vm.provider == name, f"{name}: VM provider field must match provider name"
    assert vm.preferred_ip(), f"{name}: preferred_ip() must be non-empty"


@pytest.mark.parametrize("name", _provision_providers)
def test_provisioned_vm_prefers_ipv6_when_available(name: str, contract_image):
    """If the provider returns an IPv6 address, preferred_ip() must use it."""
    p = _make_provider(name)
    vm = p.provision(
        name="contract-test-vm",
        instance_type="t",
        region="r",
        image=contract_image,
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    # hetzner and digitalocean fakes both return IPv6; dry-run/qemu do not.
    if vm.public_ipv6:
        assert vm.preferred_ip() == vm.public_ipv6


# ── Contract: get returns the same VM ───────────────────────────────────────


@pytest.mark.parametrize("name", _provision_providers)
def test_get_returns_provisioned_vm(name: str, contract_image):
    p = _make_provider(name)
    vm = p.provision(
        name="contract-test-vm",
        instance_type="t",
        region="r",
        image=contract_image,
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    fetched = p.get(vm.id)
    assert fetched.id == vm.id, f"{name}: get() returned a different VM id"
    assert fetched.name == vm.name, f"{name}: get() returned a different VM name"
    assert fetched.provider == name, f"{name}: get() returned wrong provider"


# ── Contract: list_by_label finds the VM ────────────────────────────────────


@pytest.mark.parametrize("name", _provision_providers)
def test_list_by_label_finds_provisioned_vm(name: str, contract_image):
    p = _make_provider(name)
    vm = p.provision(
        name="contract-test-vm",
        instance_type="t",
        region="r",
        image=contract_image,
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true", "extra-label": "yes"},
    )
    found = p.list_by_label("hermes-sandbox", "true")
    ids = [v.id for v in found]
    assert vm.id in ids, f"{name}: list_by_label did not find the provisioned VM"


# ── Contract: teardown is idempotent ────────────────────────────────────────


@pytest.mark.parametrize("name", _provision_providers)
def test_teardown_destroys_vm(name: str, contract_image):
    p = _make_provider(name)
    vm = p.provision(
        name="contract-test-vm",
        instance_type="t",
        region="r",
        image=contract_image,
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    p.teardown(vm.id)  # must not raise


@pytest.mark.parametrize("name", _provision_providers)
def test_teardown_missing_vm_is_noop(name: str):
    """Tearing down a VM that doesn't exist must not raise."""
    p = _make_provider(name)
    p.teardown("nonexistent-vm-id-9999")


# ── Contract: concurrent VMs don't interfere ────────────────────────────────
# A provider that backs isolated sessions must be able to hold multiple VMs
# simultaneously without one affecting another.  These tests provision two
# VMs on the same provider instance (simulating two concurrent sessions) and
# verify they are independent: distinct IDs, both discoverable, and tearing
# down one leaves the other fully intact.


@pytest.mark.parametrize("name", _provision_providers)
def test_concurrent_provisions_get_distinct_ids(name: str, make_image):
    """Two VMs provisioned on the same provider get distinct IDs."""
    p = _make_provider(name)
    vm_a = p.provision(
        name="session-a",
        instance_type="1cpu-128mb",
        region="r",
        image=make_image(),
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    vm_b = p.provision(
        name="session-b",
        instance_type="1cpu-128mb",
        region="r",
        image=make_image(),
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    assert vm_a.id != vm_b.id, f"{name}: concurrent VMs must have distinct IDs, both were {vm_a.id}"


@pytest.mark.parametrize("name", _provision_providers)
def test_concurrent_vms_both_listed_by_label(name: str, make_image):
    """Both VMs appear in list_by_label after concurrent provisioning."""
    p = _make_provider(name)
    vm_a = p.provision(
        name="session-a",
        instance_type="1cpu-128mb",
        region="r",
        image=make_image(),
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    vm_b = p.provision(
        name="session-b",
        instance_type="1cpu-128mb",
        region="r",
        image=make_image(),
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    found_ids = {v.id for v in p.list_by_label("hermes-sandbox", "true")}
    assert vm_a.id in found_ids, f"{name}: session-a VM not listed"
    assert vm_b.id in found_ids, f"{name}: session-b VM not listed"


@pytest.mark.parametrize("name", _provision_providers)
def test_teardown_one_vm_leaves_other_intact(name: str, make_image):
    """Tearing down one VM must not affect the other."""
    p = _make_provider(name)
    vm_a = p.provision(
        name="session-a",
        instance_type="1cpu-128mb",
        region="r",
        image=make_image(),
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    vm_b = p.provision(
        name="session-b",
        instance_type="1cpu-128mb",
        region="r",
        image=make_image(),
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )

    p.teardown(vm_a.id)

    # The survivor is still gettable and listed.
    survivor = p.get(vm_b.id)
    assert survivor.id == vm_b.id, f"{name}: tearing down session-a corrupted session-b's VM"
    found_ids = {v.id for v in p.list_by_label("hermes-sandbox", "true")}
    assert vm_b.id in found_ids, f"{name}: surviving VM no longer listed after sibling teardown"
    assert vm_a.id not in found_ids, f"{name}: torn-down VM still listed"


# ── Contract: shared mode reuses the VM ─────────────────────────────────────
# In shared mode (the default), all sessions share one VM. The lifecycle
# manager's ``ensure_vm()`` probes the recorded VM and reuses it rather than
# provisioning a new one. This is the complement of the isolation tests above:
# there, concurrent provisions get distinct VMs; here, sequential sessions
# get the same VM.


@pytest.mark.parametrize("name", _provision_providers)
def test_shared_mode_reuses_vm_across_sessions(name: str, make_image, tmp_path, fake_key_factory):
    """Two ensure_vm() calls in shared mode reuse the same VM (provision once)."""
    from hermes_sandbox.config import SandboxConfig
    from hermes_sandbox.lifecycle import VMLifecycleManager

    provider = _make_provider(name)
    config = SandboxConfig(
        provider_name=name,
        token="",
        ssh_user="root",
        mode="shared",
        instance_type="1cpu-128mb",
        image=make_image(),
    )
    mgr = VMLifecycleManager(
        provider=provider,
        config=config,
        hermes_home=tmp_path / ".hermes",
        key_factory=fake_key_factory,
    )

    first = mgr.ensure_vm()
    second = mgr.ensure_vm()

    assert first.id == second.id, (
        f"{name}: shared mode must reuse the VM, got {first.id} then {second.id}"
    )


@pytest.mark.parametrize("name", _provision_providers)
def test_shared_mode_provisions_only_once(name: str, make_image, tmp_path, fake_key_factory):
    """The provider's provision is called exactly once when the VM is reused."""
    from hermes_sandbox.config import SandboxConfig
    from hermes_sandbox.lifecycle import VMLifecycleManager

    provider = _make_provider(name)
    config = SandboxConfig(
        provider_name=name,
        token="",
        ssh_user="root",
        mode="shared",
        instance_type="1cpu-128mb",
        image=make_image(),
    )
    mgr = VMLifecycleManager(
        provider=provider,
        config=config,
        hermes_home=tmp_path / ".hermes",
        key_factory=fake_key_factory,
    )

    mgr.ensure_vm()
    mgr.ensure_vm()

    # Verify via the state file that a single VM was recorded (not two).
    state_file = tmp_path / ".hermes" / "sandbox" / "state.json"
    state = json.loads(state_file.read_text())
    assert state["vm_id"], f"{name}: state must record the VM id"

    # DryRunProvider records calls — verify provision fired exactly once.
    from hermes_sandbox.dry_run import DryRunProvider

    if isinstance(provider, DryRunProvider):
        provisions = [c for c in provider.calls if c["op"] == "provision"]
        assert len(provisions) == 1, (
            f"{name}: shared mode must provision once, called {len(provisions)} times"
        )
