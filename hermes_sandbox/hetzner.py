"""Hetzner Cloud provider for the sandbox plugin.

Uses the public Hetzner Cloud REST API (https://api.hetzner.cloud/v1) over
stdlib :mod:`urllib` — no third-party dependency required.  Auth is a
Bearer token (``HCLOUD_TOKEN``).

The provider creates a server with an injected SSH public key, polls until
it has a public IPv4, and returns a :class:`~hermes_sandbox.provider.VMInstance`.
Servers are labelled ``hermes-sandbox=true`` so the lifecycle manager
can discover sandboxes created by a previous Hermes process.
"""

from __future__ import annotations

import base64
import logging
from typing import Any
from urllib.parse import urlencode

from .models import ApiError, VMInstance
from .provider import RestCloudProvider, build_cloud_init_user_data, poll_until, register_provider

logger = logging.getLogger(__name__)

API_BASE = "https://api.hetzner.cloud/v1"
_DEFAULT_LABEL_KEY = "hermes-sandbox"
_DEFAULT_LABEL_VALUE = "true"


def _extract_ip(pub_net: dict[str, Any], key: str) -> str:
    """Pull a public IP from a Hetzner public_net field.

    Hetzner returns ipv4/ipv6 either as a single object ``{"ip": "..."}``
    or (older API) as a list of such objects.  IPv6 addresses carry a /64
    prefix which we strip.
    """
    val = pub_net.get(key)
    if isinstance(val, dict):
        ip = val.get("ip", "")
    elif isinstance(val, list):
        ip = ""
        for net in val:
            if isinstance(net, dict) and net.get("ip"):
                ip = net["ip"]
                break
    else:
        ip = ""
    return ip.split("/")[0] if ip else ""


def _to_vm(server: dict[str, Any]) -> VMInstance:
    """Map a Hetzner server object to a :class:`VMInstance`."""
    pub_net = server.get("public_net", {}) or {}
    return VMInstance(
        id=str(server.get("id", "")),
        name=server.get("name", ""),
        status=server.get("status", ""),
        public_ip=_extract_ip(pub_net, "ipv4"),
        public_ipv6=_extract_ip(pub_net, "ipv6"),
        region=server.get("datacenter", {}).get("location", {}).get("name", ""),
        provider="hetzner",
        extra={
            "labels": server.get("labels") or {},
            "server_type": server.get("server_type", {}).get("name", ""),
            "image": (server.get("image") or {}).get("name", "")
            if isinstance(server.get("image"), dict)
            else "",
        },
    )


@register_provider
class HetznerProvider(RestCloudProvider):
    """Hetzner Cloud implementation."""

    name = "hetzner"
    api_base = API_BASE
    token_env_keys = ("HCLOUD_TOKEN", "SANDBOX_TOKEN")
    default_region = "nbg1"
    default_image = "ubuntu-24.04"
    default_instance_type = "cx23"

    def _delete_path(self, vm_id: str) -> str:
        """Return the REST path for deleting a server by id."""
        return f"/servers/{vm_id}"

    # -- helpers -----------------------------------------------------------

    def _ensure_ssh_key(self, public_key: str, name: str = "hermes-sandbox") -> str:
        """Upload the public key (idempotent) and return its name."""
        try:
            listing = self._request("GET", "/ssh_keys", self.token)
        except ApiError:
            listing = {"ssh_keys": []}
        for key in listing.get("ssh_keys", []):
            if key.get("name") == name or key.get("public_key", "").strip() == public_key:
                return str(key["name"])
        # Upload it.
        self._request(
            "POST",
            "/ssh_keys",
            self.token,
            body={"name": name, "public_key": public_key},
        )
        return name

    # -- CloudProvider interface ------------------------------------------

    def provision(
        self,
        name: str,
        instance_type: str,
        region: str,
        image: str,
        ssh_public_key: str,
        labels: dict[str, str] | None = None,
        forward_env: dict[str, str] | None = None,
    ) -> VMInstance:
        """Create a server and block until it has a public IPv4."""
        labels = labels or {}
        labels.setdefault(_DEFAULT_LABEL_KEY, _DEFAULT_LABEL_VALUE)
        key_name = self._ensure_ssh_key(ssh_public_key)
        body = {
            "name": name,
            "server_type": instance_type,
            "location": region,
            "image": image,
            "ssh_keys": [key_name],
            "labels": labels,
            "public_net": {"enable_ipv4": True, "enable_ipv6": True},
            # Hetzner requires user_data to be base64-encoded.
            "user_data": base64.b64encode(
                build_cloud_init_user_data(
                    ssh_public_key, forward_env=forward_env
                ).encode("utf-8")
            ).decode("ascii"),
        }
        resp = self._request("POST", "/servers", self.token, body=body)
        vm = _to_vm(resp.get("server", {}))

        # Poll until a public IP is assigned.
        def _has_ip() -> bool:
            nonlocal vm
            if vm.public_ip:
                return True
            vm = self.get(vm.id)
            return bool(vm.public_ip)

        try:
            poll_until(
                _has_ip,
                interval=self.poll_interval,
                timeout=self.poll_timeout,
                sleep=self._sleep,
            )
        except TimeoutError as exc:
            raise RuntimeError(
                f"Hetzner VM {vm.id} never received a public IP within {self.poll_timeout}s"
            ) from exc
        return vm

    def get(self, vm_id: str) -> VMInstance:
        """Return the current description of a server by id."""
        resp = self._request("GET", f"/servers/{vm_id}", self.token)
        return _to_vm(resp.get("server", {}))

    def list_by_label(self, label_key: str, label_value: str) -> list[VMInstance]:
        """List servers whose ``label_selector`` matches ``label_key=label_value``."""
        path = "/servers?" + urlencode({"label_selector": f"{label_key}={label_value}"})
        resp = self._request("GET", path, self.token)
        return [_to_vm(s) for s in resp.get("servers", [])]
