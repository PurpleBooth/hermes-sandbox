"""Tests for the VM lifecycle manager (reuse, teardown, state, IPv6)."""

import json
import time

import pytest

from vm_sandbox.lifecycle import SandboxConfig, VMLifecycleManager
from vm_sandbox.provider import FakeProvider


def make_manager(tmp_path, provider=None, config=None, fake_key_factory=None,
                 clock=None):
    provider = provider or FakeProvider()
    config = config or SandboxConfig(provider_name="fake", token="", ssh_user="root")
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(exist_ok=True)
    return VMLifecycleManager(
        provider=provider,
        config=config,
        hermes_home=hermes_home,
        key_factory=fake_key_factory,
        clock=clock or time.time,
    )


def test_config_from_env_hetzner(monkeypatch):
    monkeypatch.setenv("VM_SANDBOX_PROVIDER", "hetzner")
    monkeypatch.setenv("HCLOUD_TOKEN", "tok")
    monkeypatch.setenv("VM_SANDBOX_INSTANCE_TYPE", "cpx21")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.provider_name == "hetzner"
    assert cfg.token == "tok"
    assert cfg.instance_type == "cpx21"
    assert cfg.is_configured()


def test_config_from_env_hetzner_defaults(monkeypatch):
    """Hetzner's own defaults apply when no VM_SANDBOX_* override is set."""
    monkeypatch.setenv("VM_SANDBOX_PROVIDER", "hetzner")
    monkeypatch.setenv("HCLOUD_TOKEN", "tok")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.region == "nbg1"
    assert cfg.image == "ubuntu-24.04"
    assert cfg.instance_type == "cx23"


def test_config_from_env_digitalocean_defaults(monkeypatch):
    monkeypatch.setenv("VM_SANDBOX_PROVIDER", "digitalocean")
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dotok")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.provider_name == "digitalocean"
    assert cfg.token == "dotok"
    assert cfg.region == "fra1"
    assert cfg.image == "ubuntu-24-04-x64"
    assert cfg.instance_type == "s-2vcpu-4gb"


def test_config_not_configured_without_token():
    cfg = SandboxConfig(provider_name="hetzner", token="")
    assert not cfg.is_configured()


def test_config_fake_is_configured_without_token():
    assert SandboxConfig(provider_name="fake", token="").is_configured()


def test_ensure_vm_provisions_and_records_state(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    vm = mgr.ensure_vm()
    assert vm.is_running()
    state = json.loads(mgr.state_path.read_text())
    assert state["vm_id"] == vm.id
    assert state["ssh_host"] == vm.preferred_ip()
    assert state["ssh_user"] == "root"


def test_ensure_vm_reuses_existing(tmp_path, fake_key_factory):
    provider = FakeProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    first = mgr.ensure_vm()
    second = mgr.ensure_vm()
    assert first.id == second.id
    # FakeProvider records calls; provision should only have fired once.
    provisions = [c for c in provider.calls if c["op"] == "provision"]
    assert len(provisions) == 1


def test_ensure_vm_reprovisions_when_dead(tmp_path, fake_key_factory):
    import dataclasses

    provider = FakeProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    first = mgr.ensure_vm()
    # Kill it on the provider side (VMInstance is frozen — replace, don't mutate).
    provider._vms[first.id] = dataclasses.replace(provider._vms[first.id], status="off")
    second = mgr.ensure_vm()
    assert second.id != first.id
    provisions = [c for c in provider.calls if c["op"] == "provision"]
    assert len(provisions) == 2


def test_ensure_vm_reprovisions_when_provider_forgets(tmp_path, fake_key_factory):
    provider = FakeProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    first = mgr.ensure_vm()
    # Simulate the VM vanishing from the provider entirely.
    del provider._vms[first.id]
    second = mgr.ensure_vm()
    assert second.id != first.id


def test_teardown_clears_state(tmp_path, fake_key_factory):
    provider = FakeProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    assert mgr.state_path.exists()
    assert mgr.teardown() is True
    assert not mgr.state_path.exists()
    # Idempotent: no state -> False, no crash.
    assert mgr.teardown() is False


def test_maybe_auto_teardown_respects_config(tmp_path, fake_key_factory):
    # auto_teardown_seconds=0 -> never.
    provider = FakeProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    assert mgr.maybe_auto_teardown() is False
    assert mgr.state_path.exists()

    # >0 -> tears down.
    provider2 = FakeProvider()
    cfg = SandboxConfig(provider_name="fake", token="", ssh_user="root",
                        auto_teardown_seconds=60)
    mgr2 = make_manager(tmp_path, provider=provider2, config=cfg,
                        fake_key_factory=fake_key_factory)
    mgr2.ensure_vm()
    assert mgr2.maybe_auto_teardown() is True
    assert not mgr2.state_path.exists()


def test_state_records_ipv6_and_ssh_host(tmp_path, fake_key_factory):
    import dataclasses

    provider = FakeProvider()
    # Make provision return a VM with IPv6.
    orig_provision = provider.provision

    def provision_with_v6(*a, **k):
        vm = orig_provision(*a, **k)
        return dataclasses.replace(vm, public_ipv6="2a01:db8::1")

    provider.provision = provision_with_v6
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    state = json.loads(mgr.state_path.read_text())
    assert state["public_ipv6"] == "2a01:db8::1"
    # SSH host should prefer IPv6.
    assert state["ssh_host"] == "2a01:db8::1"


def test_save_state_restricts_file_permissions(tmp_path, fake_key_factory):
    """The state file holds connection details — restrict it to the owner (0600)."""
    import os
    import stat

    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    mode = stat.S_IMODE(os.stat(mgr.state_path).st_mode)
    assert mode == 0o600


def test_status_returns_none_without_state(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    assert mgr.status() is None


def test_load_state_returns_none_on_corrupt_json(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.state_path.write_text("{not valid json")
    assert mgr._load_state() is None


def test_load_state_returns_none_on_non_dict_json(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.state_path.write_text("[1, 2, 3]")
    assert mgr._load_state() is None


def test_load_state_returns_none_on_unreadable_path(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    # Point state_path at a directory — read_text() raises IsADirectoryError (OSError).
    mgr.state_path = tmp_path
    assert mgr._load_state() is None


def test_ssh_env_returns_mapping_when_provisioned(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    env = mgr.ssh_env()
    assert env is not None
    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_SSH_HOST"]
    assert env["TERMINAL_SSH_USER"] == "root"
    assert env["TERMINAL_SSH_KEY"].endswith("hermes-vm-sandbox")


def test_ssh_env_returns_none_without_state(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    assert mgr.ssh_env() is None


def test_status_returns_sandboxstatus_value_object(tmp_path, fake_key_factory):
    from vm_sandbox.lifecycle import SandboxStatus

    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    st = mgr.status()
    assert isinstance(st, SandboxStatus)
    assert st.vm_id
    assert st.provider == "fake"
    assert st.public_ip
    assert st.alive is True
    assert st.live_status == "running"
    # Round-trips to a dict carrying the keys the specdown status grep checks.
    d = st.to_dict()
    assert d["vm_id"] == st.vm_id
    assert d["provider"] == "fake"
    assert d["public_ip"] == st.public_ip


def test_status_marks_dead_when_provider_forgets(tmp_path, fake_key_factory):
    provider = FakeProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    del provider._vms[mgr.status().vm_id]  # type: ignore[union-attr]
    st = mgr.status()
    assert st is not None
    assert st.alive is False
    assert st.error  # recorded the provider error


def test_sandboxstatus_is_frozen():
    """SandboxStatus is an immutable value object."""
    import dataclasses

    from vm_sandbox.lifecycle import SandboxStatus

    st = SandboxStatus(
        vm_id="1", provider="fake", name="n", region="r", public_ip="1.2.3.4",
        public_ipv6="", ssh_host="1.2.3.4", ssh_user="root",
        created_at=0.0, last_used=0.0, alive=True,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        st.alive = False  # type: ignore[misc]
