"""Host-side SANDBOX_NETRC helpers for slash commands and agent tools.

These operate on the *operator* (Hermes host) configuration and, when a
sandbox VM is live with SSH, can also write ``/home/hermes/.netrc`` into
the running VM without a full recreate.
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Any

from .config import (
    NetrcSpec,
    SandboxConfig,
    default_netrc_login,
    format_netrc,
)


def resolve_entries(
    specs: tuple[NetrcSpec, ...],
    env: dict[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """Resolve specs against *env* into ``(machine, login, password)`` entries.

    Specs whose password env var is missing/empty are skipped.
    """
    source = env if env is not None else dict(os.environ)
    entries: list[tuple[str, str, str]] = []
    for spec in specs:
        password = source.get(spec.password_env, "")
        if not password:
            continue
        login = spec.login or default_netrc_login(spec.machine)
        entries.append((spec.machine, login, password))
    return entries


def render_netrc(specs: tuple[NetrcSpec, ...], env: dict[str, str] | None = None) -> str | None:
    """Return a .netrc body, or None if nothing could be built."""
    entries = resolve_entries(specs, env)
    if not entries:
        return None
    return format_netrc(entries)


def show_netrc_status(config: SandboxConfig, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Return a redacted status view of the configured netrc specs.

    Passwords are never returned — only whether each password env var is set.
    """
    source = env if env is not None else dict(os.environ)
    machines = []
    for spec in config.netrc_specs:
        password_set = bool(source.get(spec.password_env, ""))
        login = spec.login or default_netrc_login(spec.machine)
        machines.append(
            {
                "machine": spec.machine,
                "login": login,
                "password_env": spec.password_env,
                "password_set": password_set,
            }
        )
    return {
        "configured": bool(config.netrc_specs),
        "machines": machines,
        "resolvable": any(m["password_set"] for m in machines),
    }


def write_netrc_via_ssh(manager: Any, content: str) -> None:
    """Write *content* to ``/home/hermes/.netrc`` on the live sandbox VM.

    Uses a single SSH remote shell invocation: pipe base64 content to avoid
    quoting pitfalls, then chown/chmod 0600.
    """
    import base64
    import subprocess

    st = manager.status()
    if not st or not st.alive:
        raise RuntimeError("No live sandbox VM — provision first or use recreate")

    env = manager.ssh_env()
    if not env:
        raise RuntimeError("SSH env not available for the live VM")

    host = env["TERMINAL_SSH_HOST"]
    user = env["TERMINAL_SSH_USER"]
    key = env.get("TERMINAL_SSH_KEY")
    port = str(env.get("TERMINAL_SSH_PORT", "22"))

    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    remote = (
        "printf '%s' "
        + shlex.quote(b64)
        + " | base64 -d > /home/hermes/.netrc"
        + " && chown hermes:hermes /home/hermes/.netrc"
        + " && chmod 0600 /home/hermes/.netrc"
    )
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        port,
    ]
    if key:
        cmd.extend(["-i", key])
    cmd.append(f"{user}@{host}")
    cmd.append(remote)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"ssh write of .netrc failed (rc={result.returncode}): {detail}")


def handle_netrc_command(argv: list[str], manager: Any | None = None) -> str:
    """Dispatch ``sandbox netrc <show|render|write|help>`` subcommands.

    *manager* is required for the ``write`` subcommand (needs a live VM).
    """
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _NETRC_HELP

    sub = argv[0]
    config = SandboxConfig.from_env(dict(os.environ))

    if sub == "show":
        status = show_netrc_status(config)
        return json.dumps(status, indent=2)

    if sub == "render":
        body = render_netrc(config.netrc_specs)
        if body is None:
            return (
                "[sandbox] No resolvable netrc entries. "
                "Set SANDBOX_NETRC and the referenced password env vars "
                "in ~/.hermes/.env "
                "(e.g. SANDBOX_NETRC=github.com:GITHUB_TOKEN)."
            )
        redacted_lines = []
        for line in body.splitlines():
            if line.startswith("password "):
                redacted_lines.append("password ***")
            else:
                redacted_lines.append(line)
        return "[sandbox] Resolved .netrc (passwords redacted):\n" + "\n".join(redacted_lines)

    if sub == "write":
        if manager is None:
            return (
                "[sandbox] Not configured. Set SANDBOX_PROVIDER and a cloud "
                "token, then run `hermes sandbox enable`."
            )
        body = render_netrc(config.netrc_specs)
        if body is None:
            return (
                "[sandbox] No resolvable netrc entries. "
                "Set SANDBOX_NETRC and the referenced password env vars first."
            )
        try:
            write_netrc_via_ssh(manager, body)
        except Exception as exc:
            return f"[sandbox] Failed to write .netrc on VM: {exc}"
        return "[sandbox] Wrote /home/hermes/.netrc on the live VM (mode 0600)."

    return f"Unknown netrc subcommand: {sub}\n\n{_NETRC_HELP}"


_NETRC_HELP = """\
sandbox netrc — manage git HTTPS credentials on the sandbox VM

Subcommands:
  show     Show configured SANDBOX_NETRC machines (passwords redacted)
  render   Preview the resolved .netrc body (passwords redacted)
  write    Write /home/hermes/.netrc onto the live VM now (mode 0600)
  help     This message

Config (in ~/.hermes/.env):
  SANDBOX_NETRC=github.com:GITHUB_TOKEN,codeberg.org:CODEBERG_TOKEN
  # or full form: github.com:x-access-token:GITHUB_TOKEN

Note: provision-time injection via cloud-init always happens when
SANDBOX_NETRC is set.  `write` is for pushing an updated .netrc onto an
already-running VM without a full recreate.
"""
