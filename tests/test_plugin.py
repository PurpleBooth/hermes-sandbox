"""Tests for the plugin entrypoint: session-start hook and slash command."""

import json

import pytest

import hermes_sandbox
from hermes_sandbox.config import SandboxConfig
from hermes_sandbox.dry_run import DryRunProvider
from hermes_sandbox.lifecycle import VMLifecycleManager


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
    # Each test starts with no cached manager and no sessions.
    monkeypatch.setattr(hermes_sandbox, "_manager", None)
    monkeypatch.setattr(hermes_sandbox, "_sessions", {})
    yield


def test_on_session_start_isolated_creates_per_session_state(tmp_path, fake_key_factory, monkeypatch):
    """In isolated mode, each session gets its own state file and VM."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(hermes_sandbox, "_get_hermes_home", lambda: hermes_home)
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_MODE", "isolated")

    hermes_sandbox._on_session_start(session_id="s1")
    hermes_sandbox._on_session_start(session_id="s2")

    s1_state = hermes_home / "sandbox" / "sessions" / "s1" / "state.json"
    s2_state = hermes_home / "sandbox" / "sessions" / "s2" / "state.json"
    assert s1_state.exists()
    assert s2_state.exists()
    # Each session has its own state file in its own directory — that's the
    # isolation guarantee.  (vm_ids may match for DryRunProvider since each
    # provider instance has its own counter; real cloud providers assign
    # unique IDs.)


def test_on_session_start_shared_uses_single_state(tmp_path, fake_key_factory, monkeypatch):
    """In shared mode, all sessions reuse one VM and one state file."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(hermes_sandbox, "_get_hermes_home", lambda: hermes_home)
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_MODE", "shared")

    hermes_sandbox._on_session_start(session_id="s1")
    hermes_sandbox._on_session_start(session_id="s2")

    shared_state = hermes_home / "sandbox" / "state.json"
    assert shared_state.exists()
    # No per-session directories.
    assert not (hermes_home / "sandbox" / "sessions").exists()


def test_on_session_end_isolated_tears_down_session_vm(tmp_path, fake_key_factory, monkeypatch):
    """Ending one isolated session tears down its VM, not the other's."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(hermes_sandbox, "_get_hermes_home", lambda: hermes_home)
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_MODE", "isolated")
    monkeypatch.setenv("SANDBOX_AUTO_TEARDOWN_SECONDS", "30")

    hermes_sandbox._on_session_start(session_id="s1")
    hermes_sandbox._on_session_start(session_id="s2")

    s1_state = hermes_home / "sandbox" / "sessions" / "s1" / "state.json"
    s2_state = hermes_home / "sandbox" / "sessions" / "s2" / "state.json"
    assert s1_state.exists()
    assert s2_state.exists()

    hermes_sandbox._on_session_end(session_id="s1")

    assert not s1_state.exists()   # s1 torn down
    assert s2_state.exists()       # s2 still running


def test_on_session_start_sets_ssh_env(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)

    hermes_sandbox._on_session_start(session_id="s1")

    import os
    assert os.environ["TERMINAL_ENV"] == "ssh"
    assert os.environ["TERMINAL_SSH_USER"] == "root"
    assert os.environ["TERMINAL_SSH_HOST"]  # set to the VM's IP
    assert os.environ["TERMINAL_SSH_KEY"].endswith("hermes-sandbox")


def test_on_session_start_failopen_when_unconfigured(monkeypatch):
    # No token, default provider hetzner -> not configured -> manager None.
    assert hermes_sandbox._manager_or_build() is None
    # Must not raise, must not touch TERMINAL_ENV.
    import os
    os.environ.pop("TERMINAL_ENV", None)
    hermes_sandbox._on_session_start(session_id="s1")
    assert "TERMINAL_ENV" not in os.environ


def test_on_session_start_failopen_on_provision_error(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)

    def boom():
        raise RuntimeError("cloud is down")
    monkeypatch.setattr(mgr, "ensure_vm", boom)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)

    import os
    os.environ.pop("TERMINAL_ENV", None)
    # Must not raise.
    hermes_sandbox._on_session_start(session_id="s1")
    assert "TERMINAL_ENV" not in os.environ


def test_on_session_end_teardowns_when_auto(tmp_path, fake_key_factory, monkeypatch):
    provider = DryRunProvider()
    config = SandboxConfig(provider_name="dry-run", token="", ssh_user="root",
                           auto_teardown_seconds=30)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    mgr = VMLifecycleManager(provider=provider, config=config,
                             hermes_home=hermes_home, key_factory=fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    assert mgr.state_path.exists()

    hermes_sandbox._on_session_end(session_id="s1")
    assert not mgr.state_path.exists()


def test_on_session_end_noop_when_auto_teardown_zero(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    hermes_sandbox._on_session_end(session_id="s1")
    assert mgr.state_path.exists()


def test_slash_list_shows_vms(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    out = hermes_sandbox._handle_slash("list")
    assert out is not None
    assert "dry-run" in out


def test_slash_list_empty_message(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    out = hermes_sandbox._handle_slash("list")
    assert out is not None
    assert "No sandbox VMs found" in out


def test_slash_help_advertises_list():
    out = hermes_sandbox._handle_slash("help")
    assert out is not None
    assert "list" in out


def test_slash_recreate_returns_new_vm(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    out = hermes_sandbox._handle_slash("recreate")
    assert out is not None
    assert "VM ready" in out


def test_slash_help_advertises_recreate():
    out = hermes_sandbox._handle_slash("help")
    assert out is not None
    assert "recreate" in out


def test_slash_status_none_when_no_manager(monkeypatch):
    out = hermes_sandbox._handle_slash("status")
    assert "Not configured" in out


def test_slash_status_and_provision(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)

    out = hermes_sandbox._handle_slash("status")
    assert "No sandbox VM recorded" in out

    out = hermes_sandbox._handle_slash("provision")
    assert "VM ready" in out

    out = hermes_sandbox._handle_slash("status")
    assert mgr.state_path.stem in out or "vm_id" in out


def test_slash_teardown(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    out = hermes_sandbox._handle_slash("teardown")
    assert "torn down" in out
    assert not mgr.state_path.exists()


def test_slash_help():
    out = hermes_sandbox._handle_slash("")
    assert "/sandbox" in out
    out2 = hermes_sandbox._handle_slash("help")
    assert "/sandbox" in out2


def test_slash_help_does_not_advertise_dead_logs():
    # `logs` read a file nothing writes — removed.  Help must not mention it.
    assert "logs" not in hermes_sandbox._handle_slash("help")


def test_slash_logs_is_unknown(tmp_path, fake_key_factory, monkeypatch):
    # `logs` was removed; with a configured manager it must be "Unknown subcommand".
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    out = hermes_sandbox._handle_slash("logs")
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

    hermes_sandbox.register(FakeCtx())
    assert "on_session_start" in registered["hooks"]
    assert "on_session_end" in registered["hooks"]
    assert "sandbox" in registered["commands"]
    assert "sandbox" in registered["cli"]
