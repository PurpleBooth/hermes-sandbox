"""Tests for the Hetzner and DigitalOcean providers (fake HTTP)."""

import pytest

import vm_sandbox.digitalocean as do
import vm_sandbox.hetzner as hz
from vm_sandbox.digitalocean import DigitalOceanProvider
from vm_sandbox.digitalocean import _to_vm as do_to_vm
from vm_sandbox.hetzner import HetznerProvider
from vm_sandbox.hetzner import _to_vm as hz_to_vm

# ── Hetzner ────────────────────────────────────────────────────────────────

HZ_SERVER = {
    "id": 42,
    "name": "hermes-sandbox",
    "status": "running",
    "public_net": {
        "ipv4": {"ip": "1.2.3.4"},
        "ipv6": {"ip": "2a01:db8::/64"},
    },
    "datacenter": {"location": {"name": "nbg1"}},
    "labels": {"hermes-vm-sandbox": "true"},
    "server_type": {"name": "cx22"},
    "image": {"name": "ubuntu-24.04"},
}


def test_hetzner_to_vm_parses_ipv4_and_ipv6():
    vm = hz_to_vm(HZ_SERVER)
    assert vm.public_ip == "1.2.3.4"
    assert vm.public_ipv6 == "2a01:db8::"  # prefix stripped
    assert vm.is_running()
    assert vm.region == "nbg1"


def test_hetzner_provision_polls_for_ip():
    calls = {"n": 0}

    def fake_request(method, path, token, body=None, timeout=30):
        if method == "GET" and path.startswith("/ssh_keys"):
            return {"ssh_keys": []}
        if method == "POST" and path == "/ssh_keys":
            return {"ssh_key": {"name": "hermes-vm-sandbox"}}
        if method == "POST" and path == "/servers":
            # Return a server with no IP yet — polling must fetch it.
            return {"server": {**HZ_SERVER, "public_net": {"ipv4": {}, "ipv6": {}}}}
        if method == "GET" and path.startswith("/servers/"):
            calls["n"] += 1
            if calls["n"] < 2:
                return {"server": {**HZ_SERVER, "public_net": {"ipv4": {}, "ipv6": {}}}}
            return {"server": HZ_SERVER}
        raise AssertionError(f"unexpected {method} {path}")

    p = HetznerProvider(token="tok", poll_interval=0, poll_timeout=5, request_fn=fake_request)
    vm = p.provision("n", "cx22", "nbg1", "ubuntu-24.04", "ssh-ed25519 K")
    assert vm.public_ip == "1.2.3.4"
    assert calls["n"] >= 2


def test_hetzner_teardown_404_is_success():
    def fake_request(method, path, token, body=None, timeout=30):
        raise RuntimeError("Hetzner API DELETE /servers/1 failed: HTTP 404 not found")

    p = HetznerProvider(token="tok", request_fn=fake_request)
    p.teardown("1")  # should not raise


def test_hetzner_teardown_other_error_raises():
    def fake_request(method, path, token, body=None, timeout=30):
        raise RuntimeError("HTTP 500 boom")

    p = HetznerProvider(token="tok", request_fn=fake_request)
    with pytest.raises(RuntimeError):
        p.teardown("1")


def test_hetzner_requires_token():
    with pytest.raises(ValueError):
        HetznerProvider(token="")


# ── DigitalOcean ───────────────────────────────────────────────────────────

DO_DROPLET = {
    "id": 99,
    "name": "hermes-sandbox",
    "status": "active",
    "networks": {
        "v4": [{"type": "public", "ip_address": "5.6.7.8"}],
        "v6": [{"type": "public", "ip_address": "2604::1"}],
    },
    "region": {"slug": "fra1"},
    "tags": ["hermes-vm-sandbox"],
    "size": {"slug": "s-2vcpu-4gb"},
    "image": {"slug": "ubuntu-24-04-x64"},
}


def test_do_to_vm_parses_ipv4_ipv6_and_normalises_active():
    vm = do_to_vm(DO_DROPLET)
    assert vm.public_ip == "5.6.7.8"
    assert vm.public_ipv6 == "2604::1"
    assert vm.is_running()  # "active" normalised to "running"


def test_do_provision_polls_until_active():
    calls = {"n": 0}

    def fake_request(method, path, token, body=None, timeout=30):
        if method == "GET" and path == "/v2/account/keys":
            return {"ssh_keys": []}
        if method == "POST" and path == "/v2/account/keys":
            return {"ssh_key": {"id": 7}}
        if method == "POST" and path == "/v2/droplets":
            return {"droplet": {**DO_DROPLET, "status": "new",
                                "networks": {"v4": [], "v6": []}}}
        if method == "GET" and path.startswith("/v2/droplets/"):
            calls["n"] += 1
            if calls["n"] < 2:
                return {"droplet": {**DO_DROPLET, "status": "new",
                                    "networks": {"v4": [], "v6": []}}}
            return {"droplet": DO_DROPLET}
        raise AssertionError(f"unexpected {method} {path}")

    p = DigitalOceanProvider(token="tok", poll_interval=0, poll_timeout=5,
                             request_fn=fake_request)
    vm = p.provision("n", "s-2vcpu-4gb", "fra1", "ubuntu-24-04-x64", "ssh-ed25519 K")
    assert vm.public_ip == "5.6.7.8"
    assert vm.is_running()


def test_do_teardown_404_is_success():
    def fake_request(method, path, token, body=None, timeout=30):
        raise RuntimeError("HTTP 404 not_found")

    p = DigitalOceanProvider(token="tok", request_fn=fake_request)
    p.teardown("1")


def test_do_requires_token():
    with pytest.raises(ValueError):
        DigitalOceanProvider(token="")
