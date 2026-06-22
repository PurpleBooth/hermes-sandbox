"""Tests for the plugin entrypoint: session-start hook and slash command."""

import json

import pytest

import vm_sandbox
from vm_sandbox.config import SandboxConfig
from vm_sandbox.dry_run import DryRunProvider
from vm_sandbox.lifecycle import VMLifecycleManager


def _make_manager(tmp_path, fake_key_factory):
    provider = DryRunProvider()
    config = SandboxConfig(provider_name="dry-run", token="", ssh_user="root")
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    return VMLifecycleManager(
        provider=provider, config=config, hermes_home=hermes_home,
        key_factory=fake_key_factory,
    )


@pytest.fixture(autouse=True)
def _reset_manager(monkeypatch):
    # Each test starts with no cached manager.
    monkeypatch.setattr(vm_sandbox, "_manager", None)
    yield


def test_on_session_start_sets_ssh_env(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(vm_sandbox, "_manager", mgr)

    vm_sandbox._on_session_start(session_id="s1")

    import os
    assert os.environ["TERMINAL_ENV"] == "ssh"
    assert os.environ["TERMINAL_SSH_USER"] == "root"
    assert os.environ["TERMINAL_SSH_HOST"]  # set to the VM's IP
    assert os.environ["TERMINAL_SSH_KEY"].endswith("hermes-vm-sandbox")


def test_on_session_start_failopen_when_unconfigured(monkeypatch):
    # No token, default provider hetzner -> not configured -> manager None.
    assert vm_sandbox._manager_or_build() is None
    # Must not raise, must not touch TERMINAL_ENV.
    import os
    os.environ.pop("TERMINAL_ENV", None)
    vm_sandbox._on_session_start(session_id="s1")
    assert "TERMINAL_ENV" not in os.environ


def test_on_session_start_failopen_on_provision_error(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)

    def boom():
        raise RuntimeError("cloud is down")
    monkeypatch.setattr(mgr, "ensure_vm", boom)
    monkeypatch.setattr(vm_sandbox, "_manager", mgr)

    import os
    os.environ.pop("TERMINAL_ENV", None)
    # Must not raise.
    vm_sandbox._on_session_start(session_id="s1")
    assert "TERMINAL_ENV" not in os.environ


def test_on_session_end_teardowns_when_auto(tmp_path, fake_key_factory, monkeypatch):
    provider = DryRunProvider()
    config = SandboxConfig(provider_name="dry-run", token="", ssh_user="root",
                           auto_teardown_seconds=30)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    mgr = VMLifecycleManager(provider=provider, config=config,
                             hermes_home=hermes_home, key_factory=fake_key_factory)
    monkeypatch.setattr(vm_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    assert mgr.state_path.exists()

    vm_sandbox._on_session_end(session_id="s1")
    assert not mgr.state_path.exists()


def test_on_session_end_noop_when_auto_teardown_zero(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(vm_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    vm_sandbox._on_session_end(session_id="s1")
    assert mgr.state_path.exists()


def test_slash_status_none_when_no_manager(monkeypatch):
    out = vm_sandbox._handle_slash("status")
    assert "Not configured" in out


def test_slash_status_and_provision(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(vm_sandbox, "_manager", mgr)

    out = vm_sandbox._handle_slash("status")
    assert "No sandbox VM recorded" in out

    out = vm_sandbox._handle_slash("provision")
    assert "VM ready" in out

    out = vm_sandbox._handle_slash("status")
    assert mgr.state_path.stem in out or "vm_id" in out


def test_slash_teardown(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(vm_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    out = vm_sandbox._handle_slash("teardown")
    assert "torn down" in out
    assert not mgr.state_path.exists()


def test_slash_help():
    out = vm_sandbox._handle_slash("")
    assert "/vm-sandbox" in out
    out2 = vm_sandbox._handle_slash("help")
    assert "/vm-sandbox" in out2


def test_slash_help_does_not_advertise_dead_logs():
    # `logs` read a file nothing writes — removed.  Help must not mention it.
    assert "logs" not in vm_sandbox._handle_slash("help")


def test_slash_logs_is_unknown(tmp_path, fake_key_factory, monkeypatch):
    # `logs` was removed; with a configured manager it must be "Unknown subcommand".
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(vm_sandbox, "_manager", mgr)
    out = vm_sandbox._handle_slash("logs")
    assert "Unknown subcommand" in out


def test_register_wires_hooks_and_commands(tmp_path, fake_key_factory):
    """register(ctx) should register the session hooks and commands."""
    registered = {"hooks": [], "commands": [], "cli": []}

    class FakeCtx:
        def register_hook(self, name, cb):
            registered["hooks"].append(name)
        def register_command(self, name, handler, description="", args_hint=""):
            registered["commands"].append(name)
        def register_cli_command(self, name, help="", setup_fn=None,
                                 handler_fn=None, description=""):
            registered["cli"].append(name)

    vm_sandbox.register(FakeCtx())
    assert "on_session_start" in registered["hooks"]
    assert "on_session_end" in registered["hooks"]
    assert "vm-sandbox" in registered["commands"]
    assert "vm-sandbox" in registered["cli"]
