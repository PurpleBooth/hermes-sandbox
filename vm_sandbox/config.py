"""Resolved configuration for the vm-sandbox plugin.

:class:`SandboxConfig` is the value object the application layer reads; it is
built from the process environment by :meth:`SandboxConfig.from_env`.  Provider
defaults and token resolution live on the provider classes (Open/Closed), so
this module only knows how to *ask* a provider for its defaults/token.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_INSTANCE_TYPE = "cx23"
DEFAULT_REGION = "nbg1"
DEFAULT_IMAGE = "ubuntu-24.04"
DEFAULT_SSH_USER = "root"


@dataclass
class SandboxConfig:
    """Resolved configuration for the vm-sandbox plugin."""

    provider_name: str = "hetzner"
    instance_type: str = DEFAULT_INSTANCE_TYPE
    region: str = DEFAULT_REGION
    image: str = DEFAULT_IMAGE
    ssh_user: str = DEFAULT_SSH_USER
    token: str = ""
    vm_name: str = "hermes-sandbox"
    # 0 = never auto-teardown on session end; >0 = teardown after N idle seconds.
    auto_teardown_seconds: int = 0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> SandboxConfig:
        def _get(key: str, default: str = "") -> str:
            return env.get(key, default)

        provider_name = _get("VM_SANDBOX_PROVIDER", "hetzner")
        token = _resolve_token(provider_name, env)
        defaults = _provider_defaults(provider_name)

        return cls(
            provider_name=provider_name,
            instance_type=_get("VM_SANDBOX_INSTANCE_TYPE", defaults["instance_type"]),
            region=_get("VM_SANDBOX_REGION", defaults["region"]),
            image=_get("VM_SANDBOX_IMAGE", defaults["image"]),
            ssh_user=_get("VM_SANDBOX_SSH_USER", DEFAULT_SSH_USER),
            token=token,
            vm_name=_get("VM_SANDBOX_NAME", "hermes-sandbox"),
            auto_teardown_seconds=int(_get("VM_SANDBOX_AUTO_TEARDOWN_SECONDS", "0") or "0"),
        )

    def is_configured(self) -> bool:
        # A token is only required for real providers; the dry-run provider
        # simulates provisioning in memory and needs no token.
        return bool(self.token) or self.provider_name == "dry-run"


def _resolve_token(provider_name: str, env: dict[str, str]) -> str:
    """Resolve the API token by delegating to the provider class.

    Falls back to ``VM_SANDBOX_TOKEN`` if the provider isn't registered yet
    (e.g. during early config loading before provider modules are imported).
    """
    try:
        from .provider import get_provider_class
        return get_provider_class(provider_name).resolve_token(env)
    except KeyError:
        return env.get("VM_SANDBOX_TOKEN") or ""


def _provider_defaults(provider_name: str) -> dict[str, str]:
    """Return the provider's default region/image/instance-type.

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
        }
    return {
        "region": cls.default_region,
        "image": cls.default_image,
        "instance_type": cls.default_instance_type,
    }
