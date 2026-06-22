"""Resolved configuration for the sandbox plugin.

:class:`SandboxConfig` is the value object the application layer reads; it is
built from the process environment by :meth:`SandboxConfig.from_env`.  Provider
defaults and token resolution live on the provider classes (Open/Closed), so
this module only knows how to *ask* a provider for its defaults/token.
"""

from __future__ import annotations

from dataclasses import dataclass

from .provider import CloudProvider

DEFAULT_SSH_USER = "root"


@dataclass(frozen=True)
class SandboxConfig:
    """Resolved configuration for the sandbox plugin."""

    provider_name: str = "hetzner"
    # Field defaults come from the base CloudProvider so adding a provider
    # (which sets its own class-level defaults) needs no edit here — no drift.
    instance_type: str = CloudProvider.default_instance_type
    region: str = CloudProvider.default_region
    image: str = CloudProvider.default_image
    ssh_user: str = DEFAULT_SSH_USER
    token: str = ""
    vm_name: str = "hermes-sandbox"
    # 0 = never auto-teardown on session end; >0 = teardown after N idle seconds.
    auto_teardown_seconds: int = 0
    # shared = one VM for all sessions (default); isolated = one VM per session.
    mode: str = "shared"

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
        )

    def is_configured(self) -> bool:
        """True when the plugin has what it needs to run.

        A token is only required for real cloud providers; the ``dry-run`` and
        ``qemu`` providers run locally and need no token.
        """
        return bool(self.token) or self.provider_name in ("dry-run", "qemu")


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
