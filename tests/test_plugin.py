"""Tests for the plugin entrypoint: session-start hook and slash command."""

import argparse

import pytest

import hermes_sandbox
from hermes_sandbox.config import SandboxConfig
from hermes_sandbox.dry_run import DryRunProvider
from hermes_sandbox.lifecycle import VMLifecycleManager


def _make_manager(tmp_path, fake_key_factory):
    provider = DryRunProvider()
    config = SandboxConfig(provider_name="dry-run", token="", ssh_user="root")
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(exist_ok=True, parents=True)
    return VMLifecycleManager(
        provider=provider,
        config=config,
        hermes_home=hermes_home,
        key_factory=fake_key_factory,
    )


@pytest.fixture(autouse=True)
def _reset_manager(monkeypatch):
    monkeypatch.setattr(hermes_sandbox, "_manager", None)
    monkeypatch.setattr(hermes_sandbox, "_sessions", {})
    yield


def test_on_session_start_isolated_creates_per_session_state(
    tmp_path, fake_key_factory, monkeypatch
):
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
    assert s1_state.exists(), "isolated mode must create a per-session state file for s1"
    assert s2_state.exists(), "isolated mode must create a per-session state file for s2"


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
    assert shared_state.exists(), "shared mode must create a single state file"
    assert not (hermes_home / "sandbox" / "sessions").exists(), (
        "shared mode must not create per-session directories"
    )


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
    assert s1_state.exists(), "s1 state must exist after session start"
    assert s2_state.exists(), "s2 state must exist after session start"

    hermes_sandbox._on_session_end(session_id="s1")

    assert not s1_state.exists(), "s1 state must be gone after its session ends"
    assert s2_state.exists(), "s2 state must survive when s1 ends"


def test_on_session_start_sets_ssh_env(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)

    hermes_sandbox._on_session_start(session_id="s1")

    import os

    assert os.environ["TERMINAL_ENV"] == "ssh", (
        f"expected TERMINAL_ENV=ssh, got {os.environ.get('TERMINAL_ENV')!r}"
    )
    assert os.environ["TERMINAL_SSH_USER"] == "root", (
        f"expected TERMINAL_SSH_USER=root, got {os.environ.get('TERMINAL_SSH_USER')!r}"
    )
    assert os.environ["TERMINAL_SSH_HOST"], "TERMINAL_SSH_HOST must be set to the VM's IP"
    assert os.environ["TERMINAL_SSH_KEY"].endswith("hermes-sandbox"), (
        f"TERMINAL_SSH_KEY must end with hermes-sandbox, got {os.environ.get('TERMINAL_SSH_KEY')!r}"
    )


def test_on_session_start_failopen_when_unconfigured(monkeypatch):
    """No token + default hetzner → not configured → manager is None, hook no-ops."""
    assert hermes_sandbox._manager_or_build() is None, "with no token the manager must not be built"
    import os

    os.environ.pop("TERMINAL_ENV", None)
    hermes_sandbox._on_session_start(session_id="s1")
    assert "TERMINAL_ENV" not in os.environ, (
        "an unconfigured plugin must not set TERMINAL_ENV (fail-open)"
    )


def test_on_session_start_failopen_on_provision_error(tmp_path, fake_key_factory, monkeypatch):
    """A provisioning failure must not crash the session (fail-open)."""
    mgr = _make_manager(tmp_path, fake_key_factory)

    def boom():
        raise RuntimeError("cloud is down")

    monkeypatch.setattr(mgr, "ensure_vm", boom)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)

    import os

    os.environ.pop("TERMINAL_ENV", None)
    hermes_sandbox._on_session_start(session_id="s1")
    assert "TERMINAL_ENV" not in os.environ, (
        "a provisioning error must not leave TERMINAL_ENV set (fail-open)"
    )


def test_on_session_end_teardowns_when_auto(tmp_path, fake_key_factory, monkeypatch):
    provider = DryRunProvider()
    config = SandboxConfig(
        provider_name="dry-run", token="", ssh_user="root", auto_teardown_seconds=30
    )
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    mgr = VMLifecycleManager(
        provider=provider, config=config, hermes_home=hermes_home, key_factory=fake_key_factory
    )
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    assert mgr.state_path.exists(), "state file must exist after provisioning"

    hermes_sandbox._on_session_end(session_id="s1")
    assert not mgr.state_path.exists(), (
        "state must be gone after session end with auto-teardown enabled"
    )


def test_on_session_end_noop_when_auto_teardown_zero(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    hermes_sandbox._on_session_end(session_id="s1")
    assert mgr.state_path.exists(), "state must survive session end when auto-teardown is zero"


def test_slash_list_shows_vms(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    out = hermes_sandbox._handle_slash("list")
    assert out is not None, "list must return a string, not None"
    assert "dry-run" in out, f"list output must mention the provider name, got: {out!r}"


def test_slash_list_empty_message(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    out = hermes_sandbox._handle_slash("list")
    assert out is not None, "list with no VMs must return a string, not None"
    assert "No sandbox VMs found" in out, (
        f"empty list must show the 'No sandbox VMs found' message, got: {out!r}"
    )


def test_slash_help_advertises_list():
    out = hermes_sandbox._handle_slash("help")
    assert out is not None, "help must return a string, not None"
    assert "list" in out, f"help must advertise the list subcommand, got: {out!r}"


def test_slash_recreate_returns_new_vm(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    out = hermes_sandbox._handle_slash("recreate")
    assert out is not None, "recreate must return a string, not None"
    assert "VM ready" in out, f"recreate must report a ready VM, got: {out!r}"


def test_slash_help_advertises_recreate():
    out = hermes_sandbox._handle_slash("help")
    assert out is not None, "help must return a string, not None"
    assert "recreate" in out, f"help must advertise recreate, got: {out!r}"


def test_slash_cleanup_no_strays(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    out = hermes_sandbox._handle_slash("cleanup")
    assert out is not None, "cleanup must return a string, not None"
    assert "No stray VMs found" in out, f"cleanup with no strays must say so, got: {out!r}"


def test_slash_help_advertises_cleanup():
    out = hermes_sandbox._handle_slash("help")
    assert out is not None, "help must return a string, not None"
    assert "cleanup" in out, f"help must advertise cleanup, got: {out!r}"


def test_slash_status_none_when_no_manager(monkeypatch):
    out = hermes_sandbox._handle_slash("status")
    assert "Not configured" in out, (
        f"status with no manager must say 'Not configured', got: {out!r}"
    )


def test_slash_status_and_provision(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)

    out = hermes_sandbox._handle_slash("status")
    assert "No sandbox VM recorded" in out, f"status before provisioning must say so, got: {out!r}"

    out = hermes_sandbox._handle_slash("provision")
    assert "VM ready" in out, f"provision must report a ready VM, got: {out!r}"

    out = hermes_sandbox._handle_slash("status")
    assert mgr.state_path.stem in out or "vm_id" in out, (
        f"status after provisioning must reference the VM, got: {out!r}"
    )


def test_slash_teardown(tmp_path, fake_key_factory, monkeypatch):
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()
    out = hermes_sandbox._handle_slash("teardown")
    assert "torn down" in out, f"teardown must confirm, got: {out!r}"
    assert not mgr.state_path.exists(), "state file must be gone after teardown"


def test_slash_help():
    out = hermes_sandbox._handle_slash("")
    assert "/sandbox" in out, f"help (empty args) must show /sandbox, got: {out!r}"
    out2 = hermes_sandbox._handle_slash("help")
    assert "/sandbox" in out2, f"help subcommand must show /sandbox, got: {out2!r}"


def test_slash_help_does_not_advertise_dead_logs():
    """The removed `logs` command must not appear in help."""
    assert "logs" not in hermes_sandbox._handle_slash("help"), (
        "help must not advertise the removed 'logs' subcommand"
    )


def test_slash_logs_is_unknown(tmp_path, fake_key_factory, monkeypatch):
    """The removed `logs` subcommand must be reported as unknown."""
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    out = hermes_sandbox._handle_slash("logs")
    assert "Unknown subcommand" in out, (
        f"the removed 'logs' must be 'Unknown subcommand', got: {out!r}"
    )


def test_register_wires_hooks_and_commands(tmp_path, fake_key_factory):
    """register(ctx) should register the session hooks and commands."""
    registered = {"hooks": [], "commands": [], "cli": []}

    class FakeCtx:
        def register_hook(self, name, cb):
            registered["hooks"].append(name)

        def register_command(self, name, handler, description="", args_hint=""):
            registered["commands"].append(name)

        def register_cli_command(
            self, name, help="", setup_fn=None, handler_fn=None, description=""
        ):
            registered["cli"].append(name)

    hermes_sandbox.register(FakeCtx())
    assert "on_session_start" in registered["hooks"], (
        f"register must wire on_session_start, got hooks={registered['hooks']!r}"
    )
    assert "on_session_end" in registered["hooks"], (
        f"register must wire on_session_end, got hooks={registered['hooks']!r}"
    )
    assert "sandbox" in registered["commands"], (
        f"register must wire the sandbox slash command, got commands={registered['commands']!r}"
    )
    assert "sandbox" in registered["cli"], (
        f"register must wire the sandbox CLI command, got cli={registered['cli']!r}"
    )


# ── CLI subcommand coverage ──────────────────────────────────────────────


def test_cli_setup_creates_all_subparsers():
    """_setup_cli must register every documented action as a subparser."""
    import argparse

    parser = argparse.ArgumentParser(prog="hermes sandbox")
    hermes_sandbox._setup_cli(parser)
    actions = [
        a
        for a in parser._subparsers._group_actions  # type: ignore[attr-defined]
        if isinstance(a, argparse._SubParsersAction)
    ]
    assert actions, "setup must create a subparsers action"
    choices = list(actions[0].choices)
    for sub in ("status", "provision", "teardown", "list", "recreate", "cleanup", "enable"):
        assert sub in choices, f"setup must register the {sub!r} subparser, got {choices!r}"


def test_cli_available_lists_providers(capsys):
    """`hermes sandbox available` must print the registered provider names."""
    rc = hermes_sandbox._handle_cli(argparse.Namespace(hermes_sandbox_action="available"))
    captured = capsys.readouterr()
    assert rc == 0, f"'available' must return exit code 0, got {rc}"
    assert "Registered providers:" in captured.out, (
        f"output must list registered providers, got: {captured.out!r}"
    )
    assert "dry-run" in captured.out, f"output must include dry-run, got: {captured.out!r}"


def test_cli_defaults_to_status_when_no_action(tmp_path, fake_key_factory, monkeypatch, capsys):
    """With no action selected, the CLI falls through to the slash handler (status)."""
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    rc = hermes_sandbox._handle_cli(argparse.Namespace())
    captured = capsys.readouterr()
    assert rc == 0, f"default action must return 0, got {rc}"
    assert "No sandbox VM recorded" in captured.out, (
        f"default (status) must show the no-VM message, got: {captured.out!r}"
    )


# ── enable slash subcommand coverage ─────────────────────────────────────


def test_slash_enable_reports_result(monkeypatch):
    """The `enable` subcommand must attempt to set terminal.backend=ssh."""
    import types

    fake_cli = types.ModuleType("hermes_cli.config")
    fake_cli.set_config_value = lambda key, val: None
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli.config", fake_cli)
    out = hermes_sandbox._handle_slash("enable")
    assert out is not None, "enable must return a string"
    assert "terminal.backend=ssh" in out, (
        f"enable must confirm terminal.backend=ssh was set, got: {out!r}"
    )


def test_slash_enable_reports_error_on_import_failure(monkeypatch):
    """When hermes_cli is unavailable, enable must surface a helpful message."""
    import builtins

    real_import = builtins.__import__

    def block_hermes_cli(name, *args, **kwargs):
        if name.startswith("hermes_cli"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_hermes_cli)
    out = hermes_sandbox._handle_slash("enable")
    assert out is not None, "enable must return a string even on failure"
    assert "Could not set" in out, f"enable must report the failure clearly, got: {out!r}"


# ── _collect_known_vm_ids coverage ───────────────────────────────────────


def test_collect_known_vm_ids_includes_singleton_and_session_files(
    tmp_path, fake_key_factory, monkeypatch
):
    """_collect_known_vm_ids must gather IDs from the repo and session files."""
    import json

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(hermes_sandbox, "_sessions", {})

    mgr = _make_manager(tmp_path, fake_key_factory)
    mgr.ensure_vm()

    sessions_dir = hermes_home / "sandbox" / "sessions" / "other-proc"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "state.json").write_text(json.dumps({"vm_id": "vm-from-other-proc"}))

    known = hermes_sandbox._collect_known_vm_ids(mgr)
    assert mgr.status() is not None, "manager must have a recorded VM"
    recorded_id = mgr.status().vm_id  # type: ignore[union-attr]
    assert recorded_id in known, f"singleton VM id must be in known set, got {known!r}"
    assert "vm-from-other-proc" in known, f"session-file VM id must be in known set, got {known!r}"


def test_collect_known_vm_ids_skips_corrupt_session_file(tmp_path, fake_key_factory, monkeypatch):
    """A corrupt session state file must be silently skipped, not crash."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(hermes_sandbox, "_sessions", {})

    mgr = _make_manager(tmp_path, fake_key_factory)
    mgr.ensure_vm()

    sessions_dir = hermes_home / "sandbox" / "sessions" / "broken"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "state.json").write_text("{corrupt json")

    known = hermes_sandbox._collect_known_vm_ids(mgr)
    assert known, "known set must not be empty (the singleton VM is known)"


def test_collect_known_vm_ids_includes_in_memory_sessions(tmp_path, fake_key_factory, monkeypatch):
    """In-memory isolated session managers contribute their VM IDs."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    session_mgr = _make_manager(tmp_path / "session", fake_key_factory)
    session_mgr.ensure_vm()
    monkeypatch.setattr(hermes_sandbox, "_sessions", {"s1": session_mgr})

    mgr = _make_manager(tmp_path, fake_key_factory)
    known = hermes_sandbox._collect_known_vm_ids(mgr)
    session_vm = session_mgr.status()
    assert session_vm is not None, "session manager must have a recorded VM"
    assert session_vm.vm_id in known, f"in-memory session VM id must be in known set, got {known!r}"


# ── Isolated session error paths ─────────────────────────────────────────


def test_on_session_start_isolated_failopen_on_error(tmp_path, fake_key_factory, monkeypatch):
    """An isolated-mode provisioning error must not crash session start."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(hermes_sandbox, "_get_hermes_home", lambda: hermes_home)
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_MODE", "isolated")

    original_build = hermes_sandbox._build_session_manager

    def failing_build(session_id, config):
        raise RuntimeError("provider construction failed")

    monkeypatch.setattr(hermes_sandbox, "_build_session_manager", failing_build)
    hermes_sandbox._on_session_start(session_id="s1")
    assert "s1" not in hermes_sandbox._sessions, (
        "a failed isolated session must not be cached in _sessions"
    )
    monkeypatch.setattr(hermes_sandbox, "_build_session_manager", original_build)


def test_on_session_end_isolated_failopen_on_error(tmp_path, fake_key_factory, monkeypatch):
    """An isolated session-end teardown error must be swallowed, not crash."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(hermes_sandbox, "_get_hermes_home", lambda: hermes_home)
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_MODE", "isolated")
    monkeypatch.setenv("SANDBOX_AUTO_TEARDOWN_SECONDS", "30")

    hermes_sandbox._on_session_start(session_id="s1")
    assert "s1" in hermes_sandbox._sessions, "session must be tracked after start"

    session_mgr = hermes_sandbox._sessions["s1"]

    def explode():
        raise RuntimeError("teardown blew up")

    monkeypatch.setattr(session_mgr, "maybe_auto_teardown", explode)
    hermes_sandbox._on_session_end(session_id="s1")
    assert "s1" not in hermes_sandbox._sessions, (
        "session must be removed from _sessions even on teardown error"
    )


def test_on_session_end_shared_failopen_on_error(tmp_path, fake_key_factory, monkeypatch):
    """A shared-mode teardown error on session end must be swallowed."""
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()

    def explode():
        raise RuntimeError("shared teardown blew up")

    monkeypatch.setattr(mgr, "maybe_auto_teardown", explode)
    hermes_sandbox._on_session_end(session_id="s1")


# ── Eager wiring on register() ───────────────────────────────────────────
def test_register_eagerly_sets_ssh_env_from_existing_state(tmp_path, fake_key_factory, monkeypatch):
    """register() must set TERMINAL_SSH_* from state.json before any hook fires.

    The agent constructor probes the terminal backend during __init__, which
    runs after plugin discovery but before the first on_session_start hook.
    If a VM already exists in state.json, register() must wire the env vars
    eagerly so the probe succeeds.
    """
    import os

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(hermes_sandbox, "_get_hermes_home", lambda: hermes_home)
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")

    # Provision a VM so state.json exists and is populated.
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()

    # Simulate a fresh process: clear the manager and env vars.
    monkeypatch.setattr(hermes_sandbox, "_manager", None)
    for k in ("TERMINAL_ENV", "TERMINAL_SSH_HOST", "TERMINAL_SSH_USER", "TERMINAL_SSH_KEY"):
        monkeypatch.delenv(k, raising=False)

    class FakeCtx:
        def register_hook(self, name, cb):
            pass

        def register_command(self, name, handler, description="", args_hint=""):
            pass

        def register_cli_command(
            self, name, help="", setup_fn=None, handler_fn=None, description=""
        ):
            pass

    hermes_sandbox.register(FakeCtx())

    assert os.environ.get("TERMINAL_ENV") == "ssh", (
        f"register() must eagerly set TERMINAL_ENV=ssh from existing state, "
        f"got {os.environ.get('TERMINAL_ENV')!r}"
    )
    assert os.environ.get("TERMINAL_SSH_HOST"), (
        "register() must eagerly set TERMINAL_SSH_HOST from existing state"
    )
    assert os.environ.get("TERMINAL_SSH_USER") == "root", (
        f"register() must eagerly set TERMINAL_SSH_USER, "
        f"got {os.environ.get('TERMINAL_SSH_USER')!r}"
    )


def test_register_does_not_set_ssh_env_when_no_state(tmp_path, monkeypatch):
    """register() must not set env vars when there's no existing state file.

    On first-ever boot there's no VM to wire up. register() should no-op and
    let on_session_start handle provisioning on the first turn.
    """
    import os

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(hermes_sandbox, "_get_hermes_home", lambda: hermes_home)
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")

    for k in ("TERMINAL_ENV", "TERMINAL_SSH_HOST", "TERMINAL_SSH_USER", "TERMINAL_SSH_KEY"):
        monkeypatch.delenv(k, raising=False)

    class FakeCtx:
        def register_hook(self, name, cb):
            pass

        def register_command(self, name, handler, description="", args_hint=""):
            pass

        def register_cli_command(
            self, name, help="", setup_fn=None, handler_fn=None, description=""
        ):
            pass

    hermes_sandbox.register(FakeCtx())

    assert "TERMINAL_ENV" not in os.environ, (
        "register() must not set TERMINAL_ENV when no state file exists"
    )


# ── pre_tool_call touch ──────────────────────────────────────────────────


def test_pre_tool_call_touches_on_terminal_tool(tmp_path, fake_key_factory, monkeypatch):
    """pre_tool_call must touch last_used when a VM-backed tool fires."""
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()

    original = mgr._repo.load()
    assert original is not None

    import time

    time.sleep(0.01)
    hermes_sandbox._on_pre_tool_call(tool_name="terminal", session_id="s1")

    updated = mgr._repo.load()
    assert updated is not None
    assert updated.last_used > original.last_used, "terminal tool call must advance last_used"


def test_pre_tool_call_ignores_non_vm_tool(tmp_path, fake_key_factory, monkeypatch):
    """pre_tool_call must NOT touch last_used for non-VM tools like skill_view."""
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    mgr.ensure_vm()

    original = mgr._repo.load()
    assert original is not None

    hermes_sandbox._on_pre_tool_call(tool_name="skill_view", session_id="s1")

    updated = mgr._repo.load()
    assert updated is not None
    assert updated.last_used == original.last_used, (
        "skill_view must not advance last_used (it doesn't use the VM)"
    )


def test_pre_tool_call_failopen_when_unconfigured(monkeypatch):
    """pre_tool_call must not crash when the plugin is not configured."""
    hermes_sandbox._on_pre_tool_call(tool_name="terminal", session_id="s1")
