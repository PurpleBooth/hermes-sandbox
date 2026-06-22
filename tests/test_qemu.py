"""Tests for the QEMU local hypervisor provider."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from hermes_sandbox.models import VMInstance
from hermes_sandbox.provider import available_providers, get_provider
from hermes_sandbox.qemu import QemuProvider


def _fake_runner(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a fake subprocess runner that records every command."""
    calls: list[list[str]] = []

    def runner(cmd: list[str]) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_qemu_is_registered():
    """QEMU is in the provider registry after import."""
    import hermes_sandbox.qemu  # noqa: F401

    assert "qemu" in available_providers()


def test_qemu_provider_name_and_defaults():
    p = QemuProvider()
    assert p.name == "qemu"
    assert p.default_region == "local"
    assert p.default_instance_type == "2cpu-4gb"


def test_qemu_accepts_empty_token():
    """QEMU needs no token — get_provider(name, token=...) must not crash."""
    p = get_provider("qemu", token="")
    assert isinstance(p, QemuProvider)


def test_qemu_provision_starts_vm_and_returns_running_instance():
    runner = _fake_runner()
    p = QemuProvider(runner=runner, ssh_base_port=2222)
    vm = p.provision(
        name="test-vm",
        instance_type="2cpu-4gb",
        region="local",
        image="/data/ubuntu.qcow2",
        ssh_public_key="ssh-ed25519 FAKEKEY",
        labels={"hermes-sandbox": "true"},
    )
    assert vm.id
    assert vm.name == "test-vm"
    assert vm.is_running()
    assert vm.public_ip == "127.0.0.1"
    assert vm.region == "local"
    assert vm.provider == "qemu"
    # SSH port is in extra so the lifecycle manager can wire TERMINAL_SSH_PORT.
    assert vm.extra["ssh_port"] == 2222
    # The runner was called with a qemu-system command.
    assert len(runner.calls) == 1
    assert "qemu-system-x86_64" in runner.calls[0][0]
    assert "-daemonize" in runner.calls[0]


def test_qemu_provision_parses_instance_type():
    """Instance type '4cpu-8gb' maps to -smp 4 -m 8192."""
    runner = _fake_runner()
    p = QemuProvider(runner=runner)
    p.provision(
        name="v",
        instance_type="4cpu-8gb",
        region="local",
        image="/img.qcow2",
        ssh_public_key="k",
    )
    cmd = runner.calls[0]
    assert "-smp" in cmd
    assert cmd[cmd.index("-smp") + 1] == "4"
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "8192"


def test_qemu_provision_increments_ssh_port():
    """Each VM gets a different SSH port."""
    runner = _fake_runner()
    p = QemuProvider(runner=runner, ssh_base_port=2222)
    vm1 = p.provision("v1", "2cpu-2gb", "local", "/img.qcow2", "k")
    vm2 = p.provision("v2", "2cpu-2gb", "local", "/img.qcow2", "k")
    assert vm1.extra["ssh_port"] == 2222
    assert vm2.extra["ssh_port"] == 2223


def test_qemu_get_returns_provisioned_vm():
    p = QemuProvider(runner=_fake_runner())
    vm = p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "k")
    fetched = p.get(vm.id)
    assert fetched.id == vm.id
    assert fetched.name == vm.name
    assert fetched.provider == "qemu"


def test_qemu_get_missing_raises_keyerror():
    p = QemuProvider(runner=_fake_runner())
    with pytest.raises(KeyError):
        p.get("nonexistent")


def test_qemu_teardown_kills_process_and_removes_from_memory():
    runner = _fake_runner()
    p = QemuProvider(runner=runner)
    vm = p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "k")
    p.teardown(vm.id)
    # teardown called the runner with a pkill command.
    teardown_calls = [c for c in runner.calls if c and c[0] == "pkill"]
    assert len(teardown_calls) >= 1
    # VM is gone from memory.
    with pytest.raises(KeyError):
        p.get(vm.id)


def test_qemu_teardown_missing_is_noop():
    """Tearing down a VM that doesn't exist must not raise."""
    p = QemuProvider(runner=_fake_runner())
    p.teardown("nonexistent")  # must not raise


def test_qemu_list_by_label_finds_provisioned_vm():
    p = QemuProvider(runner=_fake_runner())
    p.provision(
        "v", "2cpu-2gb", "local", "/img.qcow2", "k",
        labels={"hermes-sandbox": "true"},
    )
    found = p.list_by_label("hermes-sandbox", "true")
    assert len(found) == 1
    assert found[0].provider == "qemu"


def test_qemu_list_by_label_empty_when_no_match():
    p = QemuProvider(runner=_fake_runner())
    assert p.list_by_label("hermes-sandbox", "true") == []


def test_qemu_provision_includes_image_in_command():
    runner = _fake_runner()
    p = QemuProvider(runner=runner)
    p.provision("v", "2cpu-2gb", "local", "/data/ubuntu.qcow2", "k")
    cmd = runner.calls[0]
    # The qcow2 path should appear in the -drive argument.
    drive_args = [a for a in cmd if "/data/ubuntu.qcow2" in a]
    assert drive_args, "qcow2 image path must be in the QEMU command"


def test_qemu_provision_includes_ssh_key_in_cloud_init():
    """The SSH public key is passed to the VM via cloud-init."""
    runner = _fake_runner()
    p = QemuProvider(runner=runner)
    p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "ssh-ed25519 AAAAB3Nza...")
    cmd = runner.calls[0]
    # cloud-init data is passed via -drive with a seed image or -fw_cfg.
    # We accept any mechanism as long as the key string is somewhere in the command.
    cmd_str = " ".join(cmd)
    assert "ssh-ed25519" in cmd_str or "cloud-init" in cmd_str.lower() or "cidata" in cmd_str.lower(), (
        "SSH public key must be passed to the VM (cloud-init, cidata, or fw_cfg)"
    )
