"""vm-sandbox plugin — cloud-VM terminal sandbox for Hermes Agent.

On ``on_session_start`` the plugin provisions (or reuses) a cloud VM via
the configured provider, then sets the ``TERMINAL_SSH_*`` environment
variables so Hermes' built-in ``ssh`` terminal backend runs every shell
command, file operation, and ``execute_code`` call inside the VM instead
of on the host.  This lets you do Docker builds, root-level installs, and
throwaway experiments in an isolated, ephemeral machine.

Configuration (env vars; put secrets in ``~/.hermes/.env``):

  VM_SANDBOX_PROVIDER          hetzner | digitalocean   (default: hetzner)
  HCLOUD_TOKEN                 Hetzner Cloud API token  (hetzner)
  DIGITALOCEAN_TOKEN           DigitalOcean API token   (digitalocean)
                               (DO_API_TOKEN also accepted)
  VM_SANDBOX_INSTANCE_TYPE     e.g. cx23 / s-2vcpu-4gb  (provider default)
  VM_SANDBOX_REGION            e.g. nbg1 / fra1         (provider default)
  VM_SANDBOX_IMAGE             e.g. ubuntu-24.04        (provider default)
  VM_SANDBOX_SSH_USER          default: root
  VM_SANDBOX_NAME              default: hermes-sandbox
  VM_SANDBOX_AUTO_TEARDOWN_SECONDS  0 = keep VM across sessions (default);
                                    >0 = tear down on session end

The plugin leaves ``terminal.backend`` as ``ssh`` in config.yaml (set via
``hermes vm-sandbox enable``) and only manages the SSH connection details
at runtime, avoiding the config→env re-bridging race.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any

from . import digitalocean as _digitalocean  # noqa: F401

# Import providers so their @register_provider decorators run on load.
from . import hetzner as _hetzner  # noqa: F401
from .lifecycle import SandboxConfig, VMLifecycleManager
from .provider import get_provider

logger = logging.getLogger(__name__)

# Module-level cache of the lifecycle manager for the current process.
# Built lazily in on_session_start so a missing config doesn't break
# plugin load (which happens before .env is necessarily loaded).
_manager: VMLifecycleManager | None = None


def _build_manager() -> VMLifecycleManager | None:
    """Construct a VMLifecycleManager from the current environment.

    Returns None if the plugin is not configured (no token for a real
    provider), so on_session_start can no-op cleanly.
    """
    config = SandboxConfig.from_env(dict(os.environ))
    if not config.is_configured():
        return None
    try:
        provider = get_provider(config.provider_name, token=config.token)
    except Exception as exc:
        logger.warning("vm-sandbox: cannot build provider %r: %s", config.provider_name, exc)
        return None
    from hermes_constants import get_hermes_home

    return VMLifecycleManager(provider=provider, config=config, hermes_home=get_hermes_home())


def _manager_or_build() -> VMLifecycleManager | None:
    global _manager
    if _manager is None:
        _manager = _build_manager()
    return _manager


def _apply_ssh_env(manager: VMLifecycleManager) -> None:
    """Point Hermes' ssh terminal backend at the sandbox VM."""
    env = manager.ssh_env()
    if not env:
        return
    os.environ.update(env)
    os.environ.setdefault("TERMINAL_SSH_PERSISTENT", "true")
    logger.info(
        "vm-sandbox: terminal backend -> ssh %s@%s",
        env["TERMINAL_SSH_USER"], env["TERMINAL_SSH_HOST"],
    )


def _invalidate_cached_terminal_env() -> None:
    """Clear Hermes' cached terminal environment so the next ``terminal`` call
    creates a fresh SSH connection.

    ``terminal_tool._active_environments`` caches the SSH environment by
    ``task_id`` (default ``"default"``).  In a long-running process (gateway,
    dashboard) that handles multiple sessions, a stale SSH connection from a
    previous session would be reused even after the plugin updates
    ``TERMINAL_SSH_*`` env vars.  Clearing the cache forces the next terminal
    call to re-read the env vars and dial the new VM.
    """
    try:
        from tools.terminal_tool import cleanup_vm
        cleanup_vm("default")
        logger.debug("vm-sandbox: cleared cached terminal environment for 'default'")
    except Exception as exc:
        logger.debug("vm-sandbox: could not clear cached terminal env: %s", exc)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


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
    """
    manager = _manager_or_build()
    if manager is None:
        logger.debug("vm-sandbox: not configured — skipping session-start provisioning")
        return
    try:
        manager.ensure_vm()
        _apply_ssh_env(manager)
        _invalidate_cached_terminal_env()
    except Exception as exc:
        logger.warning("vm-sandbox: session-start provisioning failed: %s", exc)


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Tear down the sandbox if auto-teardown is configured."""
    manager = _manager_or_build()
    if manager is None:
        return
    try:
        manager.maybe_auto_teardown()
    except Exception as exc:
        logger.warning("vm-sandbox: session-end teardown failed: %s", exc)


# ---------------------------------------------------------------------------
# Slash command (/vm-sandbox)
# ---------------------------------------------------------------------------

_HELP = """\
/vm-sandbox — cloud-VM terminal sandbox

Subcommands:
  status      Show the current sandbox VM (or "none")
  provision   Provision/reuse a VM now (also happens on session start)
  teardown    Destroy the recorded sandbox VM
  enable      Set terminal.backend=ssh in config.yaml (run once)
  help        This message

Configure via env vars (HCLOUD_TOKEN / DIGITALOCEAN_TOKEN,
VM_SANDBOX_PROVIDER, VM_SANDBOX_INSTANCE_TYPE, ...).
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
            "[vm-sandbox] Not configured. Set VM_SANDBOX_PROVIDER and a cloud "
            "token (e.g. HCLOUD_TOKEN) in ~/.hermes/.env, then run "
            "`hermes vm-sandbox enable`."
        )

    if sub == "status":
        st = manager.status()
        if not st:
            return "[vm-sandbox] No sandbox VM recorded."
        return json.dumps(st.to_dict(), indent=2, default=str)

    if sub == "provision":
        vm = manager.ensure_vm()
        return f"[vm-sandbox] VM ready: {vm.id} @ {vm.preferred_ip()} ({vm.provider})"

    if sub == "teardown":
        ok = manager.teardown()
        return "[vm-sandbox] VM torn down." if ok else "[vm-sandbox] No VM to tear down."

    return f"Unknown subcommand: {sub}\n\n{_HELP}"


def _enable_ssh_backend() -> str:
    """Set terminal.backend=ssh via the hermes CLI config."""
    try:
        from hermes_cli.config import set_config_value

        set_config_value("terminal.backend", "ssh")
        return "[vm-sandbox] Set terminal.backend=ssh. Restart Hermes to apply."
    except Exception as exc:
        return f"[vm-sandbox] Could not set terminal.backend=ssh: {exc}\nRun: hermes config set terminal.backend ssh"


# ---------------------------------------------------------------------------
# CLI subcommand (hermes vm-sandbox ...)
# ---------------------------------------------------------------------------


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="vm_sandbox_action")
    sub.add_parser("status", help="Show the current sandbox VM")
    sub.add_parser("provision", help="Provision/reuse a VM now")
    sub.add_parser("teardown", help="Destroy the recorded sandbox VM")
    sub.add_parser("enable", help="Set terminal.backend=ssh in config.yaml")
    sub.add_parser("available", help="List registered providers")


def _handle_cli(args: argparse.Namespace) -> int:
    action = getattr(args, "vm_sandbox_action", None) or "status"
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
        "vm-sandbox",
        handler=_handle_slash,
        description="Cloud-VM terminal sandbox: provision/status/teardown.",
        args_hint="<status|provision|teardown|enable>",
    )
    ctx.register_cli_command(
        "vm-sandbox",
        help="Manage the cloud-VM terminal sandbox.",
        setup_fn=_setup_cli,
        handler_fn=_handle_cli,
        description="Provision, inspect, or tear down the cloud-VM sandbox.",
    )
