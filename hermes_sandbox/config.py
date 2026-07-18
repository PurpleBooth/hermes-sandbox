"""Resolved configuration for the sandbox plugin.

:class:`SandboxConfig` is the value object the application layer reads; it is
built from the process environment by :meth:`SandboxConfig.from_env`.  Provider
defaults and token resolution live on the provider classes (Open/Closed), so
this module only knows how to *ask* a provider for its defaults/token.
"""

from __future__ import annotations

from dataclasses import dataclass

from .provider import CloudProvider

DEFAULT_SSH_USER = CloudProvider.default_ssh_user

# Default netrc logins for known git hosts when SANDBOX_NETRC uses the
# two-field form ``machine:PASSWORD_ENV``.
_DEFAULT_NETRC_LOGINS: dict[str, str] = {
    "github.com": "x-access-token",
    "api.github.com": "x-access-token",
    "codeberg.org": "oauth2",
    "gitlab.com": "oauth2",
}


@dataclass(frozen=True)
class NetrcSpec:
    """One netrc machine mapping configured via ``SANDBOX_NETRC``.

    ``login`` is optional. When omitted, a host-specific default is used
    (e.g. ``x-access-token`` for github.com, ``oauth2`` otherwise).
    """

    machine: str
    password_env: str
    login: str | None = None


@dataclass(frozen=True)
class SandboxConfig:
    """Resolved configuration for the sandbox plugin."""

    provider_name: str = "hetzner"
    # Field defaults come from the base CloudProvider so adding a provider
    # (which sets its own class-level defaults) needs no edit here — no drift.
    instance_type: str = CloudProvider.default_instance_type
    region: str = CloudProvider.default_region
    image: str = CloudProvider.default_image
    ssh_user: str = CloudProvider.default_ssh_user
    token: str = ""
    vm_name: str = "hermes-sandbox"
    # 0 = never auto-teardown on session end; >0 = teardown after N idle seconds.
    auto_teardown_seconds: int = 0
    # shared = one VM for all sessions (default); isolated = one VM per session.
    mode: str = "shared"
    # Env var names forwarded from the caller's process into the VM at provision
    # time via cloud-init append to /etc/environment
    # (e.g. ("GITHUB_TOKEN", "CODEBERG_TOKEN")).
    forward_env: tuple[str, ...] = ()
    # Netrc machine specs resolved at provision time into /home/hermes/.netrc
    # via cloud-init write_files (0600, hermes:hermes).
    # Configured by SANDBOX_NETRC, e.g.
    # "github.com:GITHUB_TOKEN,codeberg.org:CODEBERG_TOKEN" or the full form
    # "github.com:x-access-token:GITHUB_TOKEN".
    netrc_specs: tuple[NetrcSpec, ...] = ()
    # 0 = no TTL reaping (default); >0 = tear down VM when unused for N seconds.
    vm_ttl_seconds: int = 0
    # Additional apt repository lines injected as cloud-init ``apt_sources``
    # entries (e.g. ``("deb https://packages.example.com stable main",)``).
    apt_repos: tuple[str, ...] = ()
    # Additional apt packages appended to the cloud-init ``packages`` list.
    extra_packages: tuple[str, ...] = ()
    # Custom shell commands appended to the cloud-init ``runcmd`` list.
    setup_commands: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: dict[str, str]) -> SandboxConfig:
        """Build a :class:`SandboxConfig` from ``SANDBOX_*`` environment variables.

        Provider-specific defaults (region/image/instance-type) are resolved via
        the registered provider class, so each provider declares its own defaults
        rather than centralising them here.
        """

        def _get(key: str, default: str = "") -> str:
            return env.get(key, default)

        provider_name = _get("SANDBOX_PROVIDER", "hetzner")
        token = _resolve_token(provider_name, env)
        defaults = _provider_defaults(provider_name)

        return cls(
            provider_name=provider_name,
            instance_type=_get("SANDBOX_INSTANCE_TYPE", defaults["instance_type"]),
            region=_get("SANDBOX_REGION", defaults["region"]),
            image=_get("SANDBOX_IMAGE", defaults["image"]),
            ssh_user=_get("SANDBOX_SSH_USER", defaults["ssh_user"]),
            token=token,
            vm_name=_get("SANDBOX_NAME", "hermes-sandbox"),
            auto_teardown_seconds=int(_get("SANDBOX_AUTO_TEARDOWN_SECONDS", "0") or "0"),
            mode=_get("SANDBOX_MODE", "shared"),
            forward_env=tuple(
                name.strip() for name in _get("SANDBOX_FORWARD_ENV", "").split(",") if name.strip()
            ),
            netrc_specs=_parse_netrc_specs(_get("SANDBOX_NETRC", "")),
            vm_ttl_seconds=int(_get("SANDBOX_VM_TTL_SECONDS", "0") or "0"),
            apt_repos=tuple(
                line.strip() for line in _get("SANDBOX_APT_REPOS", "").splitlines() if line.strip()
            ),
            extra_packages=tuple(
                name.strip() for name in _get("SANDBOX_PACKAGES", "").split(",") if name.strip()
            ),
            setup_commands=tuple(
                cmd.strip() for cmd in _get("SANDBOX_SETUP_CMDS", "").splitlines() if cmd.strip()
            ),
        )

    def is_configured(self) -> bool:
        """True when the plugin has what it needs to run.

        A token is only required for real cloud providers; the ``dry-run`` and
        ``qemu`` providers run locally and need no token.
        """
        return bool(self.token) or self.provider_name in ("dry-run", "qemu")


def _parse_netrc_specs(raw: str) -> tuple[NetrcSpec, ...]:
    """Parse ``SANDBOX_NETRC`` into :class:`NetrcSpec` entries.

    Accepted per-entry forms (comma-separated):

    * ``machine:PASSWORD_ENV`` — login is chosen from host defaults
      (``x-access-token`` for github.com; ``oauth2`` otherwise).
    * ``machine:login:PASSWORD_ENV`` — explicit login name.

    Whitespace around fields is trimmed. Empty segments are ignored.
    Invalid entries (wrong field count) are skipped.
    """
    specs: list[NetrcSpec] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        bits = [b.strip() for b in part.split(":")]
        if len(bits) == 2 and bits[0] and bits[1]:
            specs.append(NetrcSpec(machine=bits[0], password_env=bits[1]))
        elif len(bits) == 3 and bits[0] and bits[1] and bits[2]:
            specs.append(NetrcSpec(machine=bits[0], login=bits[1], password_env=bits[2]))
        # else: skip malformed entries rather than fail config load
    return tuple(specs)


def default_netrc_login(machine: str) -> str:
    """Return the conventional netrc login for *machine*."""
    return _DEFAULT_NETRC_LOGINS.get(machine.casefold(), "oauth2")


def format_netrc(entries: list[tuple[str, str, str]]) -> str:
    """Render ``[(machine, login, password), ...]`` as a ``.netrc`` file body.

    machines are separated by a blank line. Password values are written
    literally — callers must ensure the destination file is mode ``0600``.
    """
    blocks: list[str] = []
    for machine, login, password in entries:
        blocks.append(f"machine {machine}\nlogin {login}\npassword {password}\n")
    return "\n".join(blocks)


def _resolve_token(provider_name: str, env: dict[str, str]) -> str:
    """Resolve the API token by delegating to the provider class.

    Falls back to ``SANDBOX_TOKEN`` if the provider isn't registered yet
    (e.g. during early config loading before provider modules are imported).
    """
    try:
        from .provider import get_provider_class

        return get_provider_class(provider_name).resolve_token(env)
    except KeyError:
        return env.get("SANDBOX_TOKEN") or ""


def _provider_defaults(provider_name: str) -> dict[str, str]:
    """Return the provider's default region/image/instance-type/ssh-user.

    Defaults live on the provider class (Open/Closed) so adding a provider
    needs no edit here.  Falls back to the base ``CloudProvider`` defaults if
    the provider isn't registered yet.
    """
    from .provider import CloudProvider, get_provider_class

    try:
        cls = get_provider_class(provider_name)
    except KeyError:
        return {
            "region": CloudProvider.default_region,
            "image": CloudProvider.default_image,
            "instance_type": CloudProvider.default_instance_type,
            "ssh_user": CloudProvider.default_ssh_user,
        }
    return {
        "region": cls.default_region,
        "image": cls.default_image,
        "instance_type": cls.default_instance_type,
        "ssh_user": cls.default_ssh_user,
    }
