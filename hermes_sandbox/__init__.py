"""sandbox plugin — cloud-VM terminal sandbox for Hermes Agent.

On ``on_session_start`` the plugin provisions (or reuses) a cloud VM via
the configured provider, then sets the ``TERMINAL_SSH_*`` environment
variables so Hermes' built-in ``ssh`` terminal backend runs every shell
command, file operation, and ``execute_code`` call inside the VM instead
of on the host.  This lets you do Docker builds, root-level installs, and
throwaway experiments in an isolated, ephemeral machine.

Configuration (env vars; put secrets in ``~/.hermes/.env``):

  SANDBOX_PROVIDER          hetzner | digitalocean | dry-run | qemu   (default: hetzner)
  HCLOUD_TOKEN                 Hetzner Cloud API token  (hetzner)
  DIGITALOCEAN_TOKEN           DigitalOcean API token   (digitalocean)
                               (DO_API_TOKEN also accepted)
  SANDBOX_INSTANCE_TYPE     e.g. cx23 / s-2vcpu-4gb  (provider default)
  SANDBOX_REGION            e.g. nbg1 / fra1         (provider default)
  SANDBOX_IMAGE             e.g. ubuntu-24.04        (provider default)
  SANDBOX_SSH_USER          default: root
  SANDBOX_NAME              default: hermes-sandbox
  SANDBOX_AUTO_TEARDOWN_SECONDS  0 = keep VM across sessions (default);
                                    >0 = tear down on session end
  SANDBOX_MODE              shared (default) = one VM for all sessions;
                                    isolated = one VM per session

The plugin leaves ``terminal.backend`` as ``ssh`` in config.yaml (set via
``hermes sandbox enable``) and only manages the SSH connection details
at runtime, avoiding the config→env re-bridging race.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from . import digitalocean as _digitalocean  # noqa: F401

# Import providers so their @register_provider decorators run on load.
from . import dry_run as _dry_run  # noqa: F401
from . import hetzner as _hetzner  # noqa: F401
from . import qemu as _qemu  # noqa: F401
from .config import SandboxConfig
from .lifecycle import VMLifecycleManager
from .provider import get_provider

logger = logging.getLogger(__name__)

# Module-level cache of the lifecycle manager for the current process.
# In shared mode this is the singleton; in isolated mode the per-session
# managers live in ``_sessions`` and this is used only by slash/CLI commands.
_manager: VMLifecycleManager | None = None
# Per-session managers for isolated mode, keyed by session_id.
_sessions: dict[str, VMLifecycleManager] = {}


def _get_hermes_home() -> Path:
    """Return the Hermes home directory (wraps the hermes_constants import)."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _build_config() -> SandboxConfig | None:
    """Build a SandboxConfig from the current environment.

    Returns None if the plugin is not configured (no token for a real
    provider), so hooks can no-op cleanly.
    """
    config = SandboxConfig.from_env(dict(os.environ))
    if not config.is_configured():
        return None
    return config


def _build_manager() -> VMLifecycleManager | None:
    """Construct a shared VMLifecycleManager from the current environment."""
    config = _build_config()
    if config is None:
        return None
    try:
        provider = get_provider(config.provider_name, token=config.token)
    except Exception as exc:
        logger.warning("sandbox: cannot build provider %r: %s", config.provider_name, exc)
        return None
    return VMLifecycleManager(provider=provider, config=config, hermes_home=_get_hermes_home())


def _build_session_manager(session_id: str, config: SandboxConfig) -> VMLifecycleManager:
    """Construct a per-session VMLifecycleManager for isolated mode."""
    from .state import JsonStateRepository

    provider = get_provider(config.provider_name, token=config.token)
    hermes_home = _get_hermes_home()
    session_dir = hermes_home / "sandbox" / "sessions" / session_id
    return VMLifecycleManager(
        provider=provider,
        config=config,
        hermes_home=hermes_home,
        state_repo=JsonStateRepository(session_dir),
    )


def _manager_or_build() -> VMLifecycleManager | None:
    global _manager
    if _manager is None:
        _manager = _build_manager()
    return _manager


def _collect_known_vm_ids(manager: VMLifecycleManager) -> set[str]:
    """Collect VM IDs that local state files and active sessions reference.

    A VM is "known" (not stray) if any of these point at it:
    - the shared state file (``~/.hermes/sandbox/state.json``)
    - any isolated-session state file (``~/.hermes/sandbox/sessions/*/state.json``)
    - an in-memory session manager in ``_sessions``
    - the singleton manager's own state
    """
    known: set[str] = set()

    # Singleton manager's own state.
    state = manager._repo.load()
    if state and state.vm_id:
        known.add(state.vm_id)

    # In-memory isolated sessions.
    for mgr in _sessions.values():
        state = mgr._repo.load()
        if state and state.vm_id:
            known.add(state.vm_id)

    # Session state files on disk (covers sessions from other processes).
    sessions_dir = manager.hermes_home / "sandbox" / "sessions"
    if sessions_dir.exists():
        for state_file in sessions_dir.glob("*/state.json"):
            try:
                import json
                data = json.loads(state_file.read_text(encoding="utf-8"))
                vm_id = data.get("vm_id")
                if vm_id:
                    known.add(vm_id)
            except (ValueError, OSError):
                pass

    return known


def _apply_ssh_env(manager: VMLifecycleManager) -> None:
    """Point Hermes' ssh terminal backend at the sandbox VM."""
    env = manager.ssh_env()
    if not env:
        return
    os.environ.update(env)
    os.environ.setdefault("TERMINAL_SSH_PERSISTENT", "true")
    logger.info(
        "sandbox: terminal backend -> ssh %s@%s",
        env["TERMINAL_SSH_USER"], env["TERMINAL_SSH_HOST"],
    )


def _invalidate_cached_terminal_env(task_id: str = "default") -> None:
    """Clear Hermes' cached terminal environment so the next ``terminal`` call
    creates a fresh SSH connection.

    ``terminal_tool._active_environments`` caches the SSH environment by
    ``task_id``.  In a long-running process (gateway, dashboard) that handles
    multiple sessions, a stale SSH connection from a previous session would be
    reused even after the plugin updates ``TERMINAL_SSH_*`` env vars.  Clearing
    the cache forces the next terminal call to re-read the env vars and dial
    the new VM.
    """
    try:
        from tools.terminal_tool import cleanup_vm
        cleanup_vm(task_id)
        logger.debug("sandbox: cleared cached terminal environment for %r", task_id)
    except Exception as exc:
        logger.debug("sandbox: could not clear cached terminal env: %s", exc)


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #


def _on_session_start(
    session_id: str = "",
    model: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    """Provision/reuse the sandbox VM and wire the ssh terminal backend.

    Fail-open: any error is logged and the session continues with the
    host's existing terminal backend.  We never crash a user's session
    because the sandbox couldn't boot.

    In ``shared`` mode (default) all sessions reuse one VM — the singleton
    manager.  In ``isolated`` mode each session gets its own VM with its
    own state file under ``sessions/<session_id>/``.
    """
    task_id = session_id or "default"

    # Isolated mode: build a per-session manager for this session only.
    config = _build_config()
    if config is not None and config.mode == "isolated" and session_id:
        try:
            session_mgr = _build_session_manager(session_id, config)
            _sessions[session_id] = session_mgr
            session_mgr.ensure_vm()
            _apply_ssh_env(session_mgr)
            _invalidate_cached_terminal_env(task_id)
        except Exception as exc:
            logger.warning("sandbox: session-start provisioning failed: %s", exc)
        return

    # Shared mode: use the singleton manager (injected by tests or built from env).
    manager = _manager_or_build()
    if manager is None:
        logger.debug("sandbox: not configured — skipping session-start provisioning")
        return
    try:
        manager.ensure_vm()
        _apply_ssh_env(manager)
        _invalidate_cached_terminal_env(task_id)
    except Exception as exc:
        logger.warning("sandbox: session-start provisioning failed: %s", exc)


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Tear down the sandbox if auto-teardown is configured.

    In ``isolated`` mode, tears down only the ending session's VM.  In
    ``shared`` mode, tears down the shared VM (first session to end wins).
    """
    if session_id and session_id in _sessions:
        session_mgr = _sessions.pop(session_id)
        try:
            session_mgr.maybe_auto_teardown()
        except Exception as exc:
            logger.warning("sandbox: session-end teardown failed: %s", exc)
        return

    # Shared mode: use the singleton manager.
    manager = _manager_or_build()
    if manager is None:
        return
    try:
        manager.maybe_auto_teardown()
    except Exception as exc:
        logger.warning("sandbox: session-end teardown failed: %s", exc)


# ---------------------------------------------------------------------------
# Slash command (/sandbox)
# ---------------------------------------------------------------------------

_HELP = """\
/sandbox — cloud-VM terminal sandbox

Subcommands:
  status      Show the current sandbox VM (or "none")
  provision   Provision/reuse a VM now (also happens on session start)
  teardown    Destroy the recorded sandbox VM
  list        List all sandbox VMs on the cloud
  recreate    Destroy the current VM and provision a fresh one
  cleanup     Tear down stray VMs not tracked by any session
  enable      Set terminal.backend=ssh in config.yaml (run once)
  help        This message

Configure via env vars (HCLOUD_TOKEN / DIGITALOCEAN_TOKEN,
SANDBOX_PROVIDER, SANDBOX_INSTANCE_TYPE, ...).
"""


def _handle_slash(raw_args: str) -> str | None:
    argv = raw_args.strip().split()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _HELP
    sub = argv[0]

    # `enable` and `available` work without a configured provider — they
    # only touch config.yaml / the provider registry, not a live VM.
    if sub == "enable":
        return _enable_ssh_backend()

    manager = _manager_or_build()
    if manager is None:
        return (
            "[sandbox] Not configured. Set SANDBOX_PROVIDER and a cloud "
            "token (e.g. HCLOUD_TOKEN) in ~/.hermes/.env, then run "
            "`hermes sandbox enable`."
        )

    if sub == "status":
        st = manager.status()
        if not st:
            return "[sandbox] No sandbox VM recorded."
        return json.dumps(st.to_dict(), indent=2, default=str)

    if sub == "provision":
        vm = manager.ensure_vm()
        return f"[sandbox] VM ready: {vm.id} @ {vm.preferred_ip()} ({vm.provider})"

    if sub == "teardown":
        ok = manager.teardown()
        return "[sandbox] VM torn down." if ok else "[sandbox] No VM to tear down."

    if sub == "list":
        vms = manager.list_vms()
        if not vms:
            return "[sandbox] No sandbox VMs found."
        lines = ["[sandbox] Sandbox VMs:"]
        for vm in vms:
            state = "running" if vm.is_running() else vm.status
            lines.append(
                f"  {vm.id}  {vm.name}  {vm.provider}  {state}  {vm.preferred_ip()}  {vm.region}"
            )
        return "\n".join(lines)

    if sub == "recreate":
        vm = manager.recreate()
        return f"[sandbox] VM ready: {vm.id} @ {vm.preferred_ip()} ({vm.provider})"

    if sub == "cleanup":
        known = _collect_known_vm_ids(manager)
        destroyed = manager.cleanup_strays(known)
        if not destroyed:
            return "[sandbox] No stray VMs found."
        lines = ["[sandbox] Cleaned up stray VMs:"]
        for vm_id in destroyed:
            lines.append(f"  {vm_id}")
        return "\n".join(lines)

    return f"Unknown subcommand: {sub}\n\n{_HELP}"


def _enable_ssh_backend() -> str:
    """Set terminal.backend=ssh via the hermes CLI config."""
    try:
        from hermes_cli.config import set_config_value

        set_config_value("terminal.backend", "ssh")
        return "[sandbox] Set terminal.backend=ssh. Restart Hermes to apply."
    except Exception as exc:
        return f"[sandbox] Could not set terminal.backend=ssh: {exc}\nRun: hermes config set terminal.backend ssh"


# ---------------------------------------------------------------------------
# CLI subcommand (hermes sandbox ...)
# ---------------------------------------------------------------------------


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="hermes_sandbox_action")
    sub.add_parser("status", help="Show the current sandbox VM")
    sub.add_parser("provision", help="Provision/reuse a VM now")
    sub.add_parser("teardown", help="Destroy the recorded sandbox VM")
    sub.add_parser("list", help="List all sandbox VMs on the cloud")
    sub.add_parser("recreate", help="Destroy the current VM and provision a fresh one")
    sub.add_parser("cleanup", help="Tear down stray VMs not tracked by any session")
    sub.add_parser("enable", help="Set terminal.backend=ssh in config.yaml")
    sub.add_parser("available", help="List registered providers")


def _handle_cli(args: argparse.Namespace) -> int:
    action = getattr(args, "hermes_sandbox_action", None) or "status"
    if action == "available":
        from .provider import available_providers

        print("Registered providers: " + ", ".join(available_providers()))
        return 0
    # Reuse the slash handler for the common ops.
    print(_handle_slash(action))
    return 0


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command(
        "sandbox",
        handler=_handle_slash,
        description="Cloud-VM terminal sandbox: provision/status/teardown.",
        args_hint="<status|provision|teardown|list|recreate|cleanup|enable>",
    )
    ctx.register_cli_command(
        "sandbox",
        help="Manage the cloud-VM terminal sandbox.",
        setup_fn=_setup_cli,
        handler_fn=_handle_cli,
        description="Provision, inspect, or tear down the cloud-VM sandbox.",
    )
