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
    assert VMInstance("1", "n", "running", "1.2.3.4", "r", "p").is_running()
    assert not VMInstance("1", "n", "off", "1.2.3.4", "r", "p").is_running()


def test_vminstance_preferred_ip_prefers_ipv6():
    vm = VMInstance("1", "n", "running", "1.2.3.4", "r", "p", public_ipv6="::1")
    assert vm.preferred_ip() == "::1"
    vm4 = VMInstance("1", "n", "running", "1.2.3.4", "r", "p")
    assert vm4.preferred_ip() == "1.2.3.4"


def test_vminstance_from_dict_drops_unknown_keys():
    """from_dict is the inverse of to_dict: unknown keys are ignored, not folded."""
    vm = VMInstance.from_dict({
        "id": "1", "name": "n", "status": "running",
        "public_ip": "1.2.3.4", "region": "r", "provider": "p",
        "bogus": "dropped",
    })
    assert vm.id == "1"
    assert vm.extra == {}  # unknown key was NOT folded into extra


def test_vminstance_from_dict_preserves_extra():
    vm = VMInstance.from_dict({
        "id": "1", "name": "n", "status": "running",
        "public_ip": "1.2.3.4", "region": "r", "provider": "p",
        "extra": {"foo": "bar"},
    })
    assert vm.extra == {"foo": "bar"}


def test_vminstance_roundtrip():
    vm = VMInstance("1", "n", "running", "1.2.3.4", "r", "p", public_ipv6="::1",
                    extra={"foo": "bar"})
    d = vm.to_dict()
    assert d["public_ipv6"] == "::1"
    restored = VMInstance.from_dict(d)
    assert restored.public_ipv6 == "::1"
    assert restored.extra["foo"] == "bar"


def test_registry_has_dry_run_hetzner_digitalocean():
    names = available_providers()
    assert "dry-run" in names
    # hetzner + digitalocean register on import of their modules.
    import hermes_sandbox.digitalocean  # noqa: F401
    import hermes_sandbox.hetzner  # noqa: F401
    assert "hetzner" in available_providers()
    assert "digitalocean" in available_providers()


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
            def list_by_label(self, k, v): return []


def test_dry_run_provider_provision_get_teardown():
    from hermes_sandbox.dry_run import DryRunProvider

    p = DryRunProvider()
    vm = p.provision("v", "t", "r", "img", "ssh-key", labels={"x": "y"})
    assert vm.is_running()
    assert vm.public_ip
    # UUID-style id (like a real cloud provider would assign).
    assert len(vm.id) == 36, f"expected UUID, got {vm.id!r}"
    assert vm.id.count("-") == 4
    assert p.get(vm.id).id == vm.id
    assert p.list_by_label("x", "y") == [vm]
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
    assert calls["n"] == 3
    assert len(slept) == 2  # slept between tries, not after success


def test_poll_until_raises_on_timeout():
    from hermes_sandbox.provider import poll_until

    slept: list[float] = []
    # A clock that advances past the deadline on the second read.
    ticks = iter([0.0, 100.0, 100.0])

    def fake_clock() -> float:
        return next(ticks)

    with pytest.raises(TimeoutError):
        poll_until(lambda: False, interval=0, timeout=1,
                   sleep=slept.append, clock=fake_clock)
    assert slept == []  # never slept — deadline already passed after first check
