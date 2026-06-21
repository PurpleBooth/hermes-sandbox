"""Tests for the provider abstraction and registry."""

import pytest

from vm_sandbox.provider import (
    CloudProvider,
    FakeProvider,
    VMInstance,
    available_providers,
    get_provider,
    register_provider,
)


def test_vminstance_is_running():
    assert VMInstance("1", "n", "running", "1.2.3.4", "r", "p").is_running()
    assert not VMInstance("1", "n", "off", "1.2.3.4", "r", "p").is_running()


def test_vminstance_preferred_ip_prefers_ipv6():
    vm = VMInstance("1", "n", "running", "1.2.3.4", "r", "p", public_ipv6="::1")
    assert vm.preferred_ip() == "::1"
    vm4 = VMInstance("1", "n", "running", "1.2.3.4", "r", "p")
    assert vm4.preferred_ip() == "1.2.3.4"


def test_vminstance_roundtrip():
    vm = VMInstance("1", "n", "running", "1.2.3.4", "r", "p", public_ipv6="::1",
                    extra={"foo": "bar"})
    d = vm.to_dict()
    assert d["public_ipv6"] == "::1"
    restored = VMInstance.from_dict(d)
    assert restored.public_ipv6 == "::1"
    assert restored.extra["foo"] == "bar"


def test_registry_has_fake_hetzner_digitalocean():
    names = available_providers()
    assert "fake" in names
    # hetzner + digitalocean register on import of their modules.
    import vm_sandbox.digitalocean  # noqa: F401
    import vm_sandbox.hetzner  # noqa: F401
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


def test_fake_provider_provision_get_teardown():
    p = get_provider("fake")
    vm = p.provision("v", "t", "r", "img", "ssh-key", labels={"x": "y"})
    assert vm.is_running()
    assert vm.public_ip
    assert p.get(vm.id).id == vm.id
    assert p.list_by_label("x", "y") == [vm]
    p.teardown(vm.id)
    with pytest.raises(KeyError):
        p.get(vm.id)
