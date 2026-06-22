"""Tests for the provider abstraction and registry."""

import dataclasses

import pytest

from hermes_sandbox.models import VMInstance
from hermes_sandbox.provider import (
    CloudProvider,
    available_providers,
    get_provider,
    register_provider,
)


def test_vminstance_is_frozen():
    """VMInstance is an immutable value object — fields can't be reassigned."""
    vm = VMInstance("1", "n", "running", "1.2.3.4", "r", "p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        vm.status = "off"  # type: ignore[misc]


def test_vminstance_is_running():
    assert VMInstance("1", "n", "running", "1.2.3.4", "r", "p").is_running(), (
        "'running' status must be considered running"
    )
    assert not VMInstance("1", "n", "off", "1.2.3.4", "r", "p").is_running(), (
        "'off' status must not be considered running"
    )


def test_vminstance_preferred_ip_prefers_ipv6():
    vm = VMInstance("1", "n", "running", "1.2.3.4", "r", "p", public_ipv6="::1")
    assert vm.preferred_ip() == "::1", (
        f"preferred_ip must return IPv6 when present, got {vm.preferred_ip()!r}"
    )
    vm4 = VMInstance("1", "n", "running", "1.2.3.4", "r", "p")
    assert vm4.preferred_ip() == "1.2.3.4", (
        f"preferred_ip must fall back to IPv4, got {vm4.preferred_ip()!r}"
    )


def test_vminstance_from_dict_drops_unknown_keys():
    """from_dict is the inverse of to_dict: unknown keys are ignored, not folded."""
    vm = VMInstance.from_dict(
        {
            "id": "1",
            "name": "n",
            "status": "running",
            "public_ip": "1.2.3.4",
            "region": "r",
            "provider": "p",
            "bogus": "dropped",
        }
    )
    assert vm.id == "1", f"expected id '1', got {vm.id!r}"
    assert vm.extra == {}, f"unknown keys must not be folded into extra, got {vm.extra!r}"


def test_vminstance_from_dict_preserves_extra():
    vm = VMInstance.from_dict(
        {
            "id": "1",
            "name": "n",
            "status": "running",
            "public_ip": "1.2.3.4",
            "region": "r",
            "provider": "p",
            "extra": {"foo": "bar"},
        }
    )
    assert vm.extra == {"foo": "bar"}, f"extra must be preserved, got {vm.extra!r}"


def test_vminstance_roundtrip():
    vm = VMInstance(
        "1", "n", "running", "1.2.3.4", "r", "p", public_ipv6="::1", extra={"foo": "bar"}
    )
    d = vm.to_dict()
    assert d["public_ipv6"] == "::1", f"to_dict must expose public_ipv6, got {d['public_ipv6']!r}"
    restored = VMInstance.from_dict(d)
    assert restored.public_ipv6 == "::1", (
        f"roundtrip must preserve public_ipv6, got {restored.public_ipv6!r}"
    )
    assert restored.extra["foo"] == "bar", f"roundtrip must preserve extra, got {restored.extra!r}"


def test_registry_has_dry_run_hetzner_digitalocean():
    names = available_providers()
    assert "dry-run" in names, f"dry-run must be registered, got {names!r}"
    import hermes_sandbox.digitalocean  # noqa: F401
    import hermes_sandbox.hetzner  # noqa: F401

    assert "hetzner" in available_providers(), "hetzner must register on import"
    assert "digitalocean" in available_providers(), "digitalocean must register on import"


def test_get_provider_unknown_raises():
    with pytest.raises(KeyError):
        get_provider("nonexistent")


def test_register_provider_requires_name():
    with pytest.raises(ValueError):

        @register_provider
        class Nameless(CloudProvider):
            name = ""

            def provision(self, *a, **k): ...
            def get(self, vm_id): ...
            def teardown(self, vm_id): ...
            def list_by_label(self, k, v):
                return []


def test_dry_run_provider_provision_get_teardown():
    from hermes_sandbox.dry_run import DryRunProvider

    p = DryRunProvider()
    vm = p.provision("v", "t", "r", "img", "ssh-key", labels={"x": "y"})
    assert vm.is_running(), f"provisioned VM must be running, status={vm.status!r}"
    assert vm.public_ip, "provisioned VM must have a public IP"
    assert len(vm.id) == 36, f"expected a 36-char UUID, got {vm.id!r}"
    assert vm.id.count("-") == 4, f"expected 4 hyphens in the UUID, got {vm.id!r}"
    assert p.get(vm.id).id == vm.id, "get() must return the same VM by id"
    assert p.list_by_label("x", "y") == [vm], "list_by_label must find the provisioned VM"
    p.teardown(vm.id)
    with pytest.raises(KeyError):
        p.get(vm.id)


def test_poll_until_returns_when_predicate_true():
    from hermes_sandbox.provider import poll_until

    calls = {"n": 0}

    def pred() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    slept: list[float] = []
    poll_until(pred, interval=0, timeout=5, sleep=slept.append)
    assert calls["n"] == 3, f"predicate must be called exactly 3 times, got {calls['n']}"
    assert len(slept) == 2, f"must sleep between tries (2 sleeps), got {len(slept)}"


def test_poll_until_raises_on_timeout():
    from hermes_sandbox.provider import poll_until

    slept: list[float] = []
    ticks = iter([0.0, 100.0, 100.0])

    def fake_clock() -> float:
        return next(ticks)

    with pytest.raises(TimeoutError):
        poll_until(lambda: False, interval=0, timeout=1, sleep=slept.append, clock=fake_clock)
    assert slept == [], "must never sleep when the deadline already passed after the first check"
