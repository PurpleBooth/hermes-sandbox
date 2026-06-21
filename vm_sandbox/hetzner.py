"""Hetzner Cloud provider for the vm-sandbox plugin.

Uses the public Hetzner Cloud REST API (https://api.hetzner.cloud/v1) over
stdlib :mod:`urllib` — no third-party dependency required.  Auth is a
Bearer token (``HCLOUD_TOKEN``).

The provider creates a server with an injected SSH public key, polls until
it has a public IPv4, and returns a :class:`~vm_sandbox.provider.VMInstance`.
Servers are labelled ``hermes-vm-sandbox=true`` so the lifecycle manager
can discover sandboxes created by a previous Hermes process.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from .provider import (
    CloudProvider,
    RequestFn,
    VMInstance,
    register_provider,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.hetzner.cloud/v1"
_DEFAULT_LABEL_KEY = "hermes-vm-sandbox"
_DEFAULT_LABEL_VALUE = "true"


def _request(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Perform an authenticated Hetzner API request, returning parsed JSON."""
    url = f"{API_BASE}/{path.lstrip('/')}"
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Hetzner API {method} {path} failed: HTTP {exc.code} {detail}"
        ) from exc


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
class HetznerProvider(CloudProvider):
    """Hetzner Cloud implementation."""

    name = "hetzner"
    token_env_keys = ("HCLOUD_TOKEN", "VM_SANDBOX_TOKEN")

    def __init__(
        self,
        token: str,
        poll_interval: float = 3.0,
        poll_timeout: int = 180,
        request_fn: RequestFn | None = None,
    ) -> None:
        if not token:
            raise ValueError("HetznerProvider requires a non-empty token")
        self.token = token
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        # request_fn is injectable for tests; defaults to the real _request.
        self._request: RequestFn = request_fn or _request

    # -- helpers -----------------------------------------------------------

    def _ensure_ssh_key(self, public_key: str, name: str = "hermes-vm-sandbox") -> str:
        """Upload the public key (idempotent) and return its name."""
        # Try to find an existing key with this name or fingerprint.
        try:
            listing = self._request("GET", "/ssh_keys", self.token)
        except RuntimeError:
            listing = {"ssh_keys": []}
        for key in listing.get("ssh_keys", []):
            if key.get("name") == name or key.get("public_key", "").strip() == public_key:
                return str(key["name"])
        # Upload it.
        self._request(
            "POST", "/ssh_keys", self.token,
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
    ) -> VMInstance:
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
        }
        resp = self._request("POST", "/servers", self.token, body=body)
        server = resp.get("server", {})
        vm = _to_vm(server)
        # Poll until a public IP is assigned.
        deadline = time.monotonic() + self.poll_timeout
        while not vm.public_ip and time.monotonic() < deadline:
            time.sleep(self.poll_interval)
            vm = self.get(vm.id)
        if not vm.public_ip:
            raise RuntimeError(
                f"Hetzner VM {vm.id} never received a public IP within {self.poll_timeout}s"
            )
        return vm

    def get(self, vm_id: str) -> VMInstance:
        resp = self._request("GET", f"/servers/{vm_id}", self.token)
        return _to_vm(resp.get("server", {}))

    def teardown(self, vm_id: str) -> None:
        try:
            self._request("DELETE", f"/servers/{vm_id}", self.token)
        except RuntimeError as exc:
            # 404 / "not found" is a successful teardown.
            if "HTTP 404" in str(exc) or "not_found" in str(exc).lower():
                return
            raise

    def list_by_label(self, label_key: str, label_value: str) -> list[VMInstance]:
        resp = self._request(
            "GET", f"/servers?label_selector={label_key}={label_value}", self.token
        )
        return [_to_vm(s) for s in resp.get("servers", [])]
