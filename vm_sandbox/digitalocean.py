"""DigitalOcean provider for the vm-sandbox plugin.

Uses the DigitalOcean REST API v2 (https://api.digitalocean.com/v2) over
stdlib :mod:`urllib`.  Auth is a Bearer token (``DIGITALOCEAN_TOKEN`` /
``DO_API_TOKEN``).  Droplets are tagged ``hermes-vm-sandbox`` so the
lifecycle manager can discover sandboxes from a previous process.

Droplet creation is async: the API returns a Droplet in ``new`` state, and
we poll ``GET /v2/droplets/<id>`` until ``status == "active"`` and a public
IPv4 is present in the network array.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .provider import (
    ApiError,
    CloudProvider,
    RequestFn,
    VMInstance,
    poll_until,
    register_provider,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.digitalocean.com/v2"
_TAG = "hermes-vm-sandbox"


def _request(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
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
        raise ApiError(method, path, status_code=exc.code, detail=detail) from exc


def _public_ipv4(droplet: dict[str, Any]) -> str:
    for net in droplet.get("networks", {}).get("v4", []) or []:
        if net.get("type") == "public" and net.get("ip_address"):
            return str(net["ip_address"])
    return ""


def _public_ipv6(droplet: dict[str, Any]) -> str:
    for net in droplet.get("networks", {}).get("v6", []) or []:
        if net.get("type") == "public" and net.get("ip_address"):
            return str(net["ip_address"])
    return ""


def _to_vm(droplet: dict[str, Any]) -> VMInstance:
    return VMInstance(
        id=str(droplet.get("id", "")),
        name=droplet.get("name", ""),
        status=droplet.get("status", ""),  # "new" | "active" | "off" | "archive"
        public_ip=_public_ipv4(droplet),
        public_ipv6=_public_ipv6(droplet),
        region=droplet.get("region", {}).get("slug", ""),
        provider="digitalocean",
        extra={
            "tags": droplet.get("tags") or [],
            "size": droplet.get("size", {}).get("slug", ""),
            "image": (droplet.get("image") or {}).get("slug", "")
            if isinstance(droplet.get("image"), dict)
            else "",
        },
    )


@register_provider
class DigitalOceanProvider(CloudProvider):
    """DigitalOcean implementation."""

    name = "digitalocean"
    token_env_keys = ("DIGITALOCEAN_TOKEN", "DO_API_TOKEN", "VM_SANDBOX_TOKEN")
    default_region = "fra1"
    default_image = "ubuntu-24-04-x64"
    default_instance_type = "s-2vcpu-4gb"

    def __init__(
        self,
        token: str,
        poll_interval: float = 3.0,
        poll_timeout: int = 180,
        request_fn: RequestFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ValueError("DigitalOceanProvider requires a non-empty token")
        self.token = token
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._request: RequestFn = request_fn or _request
        self._sleep: Callable[[float], None] = sleep

    # CloudProvider maps "running" to is_running(); DO uses "active".
    # Override the status normalisation by reusing VMInstance._RUNNING_STATUSES
    # via a thin adapter: we translate "active" -> "running" in _to_vm? No —
    # cleaner to extend the running set.  We do that by post-processing.

    def _to_vm_running_aware(self, droplet: dict[str, Any]) -> VMInstance:
        vm = _to_vm(droplet)
        # DO uses "active"; normalise so VMInstance.is_running() is True.
        if vm.status == "active":
            vm = replace(vm, status="running")
        return vm

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
        key_id = self._ensure_ssh_key(ssh_public_key)
        body = {
            "name": name,
            "region": region,
            "size": instance_type,
            "image": image,
            "ssh_keys": [key_id],
            "tags": [_TAG],
            "backups": False,
            "ipv6": True,
        }
        resp = self._request("POST", "/droplets", self.token, body=body)
        vm = self._to_vm_running_aware(resp.get("droplet", {}))

        # Poll until active with a public IP.
        def _ready() -> bool:
            nonlocal vm
            if vm.public_ip and vm.is_running():
                return True
            vm = self.get(vm.id)
            return bool(vm.public_ip and vm.is_running())

        try:
            poll_until(
                _ready,
                interval=self.poll_interval,
                timeout=self.poll_timeout,
                sleep=self._sleep,
            )
        except TimeoutError as exc:
            raise RuntimeError(
                f"DigitalOcean droplet {vm.id} never became active with an IP "
                f"within {self.poll_timeout}s"
            ) from exc
        return vm

    def get(self, vm_id: str) -> VMInstance:
        resp = self._request("GET", f"/droplets/{vm_id}", self.token)
        return self._to_vm_running_aware(resp.get("droplet", {}))

    def teardown(self, vm_id: str) -> None:
        try:
            self._request("DELETE", f"/droplets/{vm_id}", self.token)
        except ApiError as exc:
            # 404 (already gone) is a successful teardown; anything else propagates.
            if exc.status_code == 404:
                return
            raise

    def list_by_label(self, label_key: str, label_value: str) -> list[VMInstance]:
        # DigitalOcean tags carry no value component — the tag name is the label
        # key.  Query by the caller's label_key so a non-default tag is honoured.
        resp = self._request("GET", f"/droplets?tag_name={label_key}", self.token)
        return [self._to_vm_running_aware(d) for d in resp.get("droplets", [])]

    # -- helpers -----------------------------------------------------------

    def _ensure_ssh_key(self, public_key: str, name: str = "hermes-vm-sandbox") -> str:
        """Upload the public key (idempotent) and return its DO key id (str)."""
        try:
            listing = self._request("GET", "/account/keys", self.token)
        except RuntimeError:
            listing = {"ssh_keys": []}
        for key in listing.get("ssh_keys", []):
            if key.get("name") == name or key.get("public_key", "").strip() == public_key:
                return str(key["id"])
        resp = self._request(
            "POST",
            "/account/keys",
            self.token,
            body={"name": name, "public_key": public_key},
        )
        return str(resp.get("ssh_key", {}).get("id", ""))
