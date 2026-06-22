"""Contract tests — every registered provider must satisfy the same lifecycle.

These tests run the same suite of assertions against each provider (dry-run,
hetzner, digitalocean) so that adding a provider automatically inherits
the full contract.  Real cloud providers are exercised with injected fake
HTTP (``request_fn``) — no network calls are made.
"""

from __future__ import annotations

from typing import Any

import pytest

from hermes_sandbox.provider import CloudProvider, available_providers, get_provider

# ── Fake HTTP payloads for cloud providers ──────────────────────────────────

_HZ_SERVER: dict[str, Any] = {
    "id": 42,
    "name": "contract-test-vm",
    "status": "running",
    "public_net": {
        "ipv4": {"ip": "1.2.3.4"},
        "ipv6": {"ip": "2a01:db8::/64"},
    },
    "datacenter": {"location": {"name": "nbg1"}},
    "labels": {"hermes-sandbox": "true"},
    "server_type": {"name": "cx23"},
    "image": {"name": "ubuntu-24.04"},
}

_DO_DROPLET: dict[str, Any] = {
    "id": 99,
    "name": "contract-test-vm",
    "status": "active",
    "networks": {
        "v4": [{"type": "public", "ip_address": "5.6.7.8"}],
        "v6": [{"type": "public", "ip_address": "2604::1"}],
    },
    "region": {"slug": "fra1"},
    "tags": ["hermes-sandbox"],
    "size": {"slug": "s-2vcpu-4gb"},
    "image": {"slug": "ubuntu-24-04-x64"},
}


def _hz_request(
    method: str, path: str, token: str, body: dict | None = None, timeout: int = 30
) -> dict[str, Any]:
    """Fake Hetzner API — returns canned responses for the full lifecycle."""
    if method == "GET" and path.startswith("/ssh_keys"):
        return {"ssh_keys": []}
    if method == "POST" and path == "/ssh_keys":
        return {"ssh_key": {"name": "hermes-sandbox"}}
    if method == "POST" and path == "/servers":
        return {"server": _HZ_SERVER}
    if method == "GET" and path.startswith("/servers/"):
        return {"server": _HZ_SERVER}
    if method == "DELETE" and path.startswith("/servers/"):
        return {}
    if method == "GET" and path.startswith("/servers?label_selector="):
        return {"servers": [_HZ_SERVER]}
    raise AssertionError(f"unexpected Hetzner request: {method} {path}")


def _do_request(
    method: str, path: str, token: str, body: dict | None = None, timeout: int = 30
) -> dict[str, Any]:
    """Fake DigitalOcean API — returns canned responses for the full lifecycle."""
    if method == "GET" and path == "/account/keys":
        return {"ssh_keys": []}
    if method == "POST" and path == "/account/keys":
        return {"ssh_key": {"id": 7}}
    if method == "POST" and path == "/droplets":
        return {"droplet": _DO_DROPLET}
    if method == "GET" and path.startswith("/droplets/"):
        return {"droplet": _DO_DROPLET}
    if method == "DELETE" and path.startswith("/droplets/"):
        return {}
    if method == "GET" and path.startswith("/droplets?tag_name="):
        return {"droplets": [_DO_DROPLET]}
    raise AssertionError(f"unexpected DigitalOcean request: {method} {path}")


# ── Provider factory ────────────────────────────────────────────────────────


def _make_provider(name: str) -> CloudProvider:
    """Build a provider instance with fake HTTP where needed."""
    if name == "dry-run":
        return get_provider("dry-run", token="")
    if name == "hetzner":
        from hermes_sandbox.hetzner import HetznerProvider

        return HetznerProvider(
            token="tok",
            poll_interval=0,
            poll_timeout=5,
            request_fn=_hz_request,
        )
    if name == "digitalocean":
        from hermes_sandbox.digitalocean import DigitalOceanProvider

        return DigitalOceanProvider(
            token="tok",
            poll_interval=0,
            poll_timeout=5,
            request_fn=_do_request,
        )
    raise ValueError(f"no factory for provider {name!r}")


# Ensure all providers are registered before collecting parametrized tests.
import hermes_sandbox.digitalocean  # noqa: F401, E402
import hermes_sandbox.hetzner  # noqa: F401, E402

_PROVIDERS = available_providers()


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


@pytest.mark.parametrize("name", _PROVIDERS)
def test_provision_returns_running_vm_with_all_fields(name: str):
    p = _make_provider(name)
    vm = p.provision(
        name="contract-test-vm",
        instance_type="test-size",
        region="test-region",
        image="test-image",
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


@pytest.mark.parametrize("name", _PROVIDERS)
def test_provisioned_vm_prefers_ipv6_when_available(name: str):
    """If the provider returns an IPv6 address, preferred_ip() must use it."""
    p = _make_provider(name)
    vm = p.provision(
        name="contract-test-vm",
        instance_type="t",
        region="r",
        image="i",
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    # hetzner and digitalocean fakes both return IPv6; fake provider does not.
    if vm.public_ipv6:
        assert vm.preferred_ip() == vm.public_ipv6


# ── Contract: get returns the same VM ───────────────────────────────────────


@pytest.mark.parametrize("name", _PROVIDERS)
def test_get_returns_provisioned_vm(name: str):
    p = _make_provider(name)
    vm = p.provision(
        name="contract-test-vm",
        instance_type="t",
        region="r",
        image="i",
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    fetched = p.get(vm.id)
    assert fetched.id == vm.id, f"{name}: get() returned a different VM id"
    assert fetched.name == vm.name, f"{name}: get() returned a different VM name"
    assert fetched.provider == name, f"{name}: get() returned wrong provider"


# ── Contract: list_by_label finds the VM ────────────────────────────────────


@pytest.mark.parametrize("name", _PROVIDERS)
def test_list_by_label_finds_provisioned_vm(name: str):
    p = _make_provider(name)
    vm = p.provision(
        name="contract-test-vm",
        instance_type="t",
        region="r",
        image="i",
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true", "extra-label": "yes"},
    )
    found = p.list_by_label("hermes-sandbox", "true")
    ids = [v.id for v in found]
    assert vm.id in ids, f"{name}: list_by_label did not find the provisioned VM"


# ── Contract: teardown is idempotent ────────────────────────────────────────


@pytest.mark.parametrize("name", _PROVIDERS)
def test_teardown_destroys_vm(name: str):
    p = _make_provider(name)
    vm = p.provision(
        name="contract-test-vm",
        instance_type="t",
        region="r",
        image="i",
        ssh_public_key="ssh-ed25519 K",
        labels={"hermes-sandbox": "true"},
    )
    p.teardown(vm.id)  # must not raise


@pytest.mark.parametrize("name", _PROVIDERS)
def test_teardown_missing_vm_is_noop(name: str):
    """Tearing down a VM that doesn't exist must not raise."""
    p = _make_provider(name)
    p.teardown("nonexistent-vm-id-9999")
