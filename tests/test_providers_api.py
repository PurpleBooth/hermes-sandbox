"""Tests for the Hetzner and DigitalOcean providers (fake HTTP)."""

import pytest

import hermes_sandbox.digitalocean as do
import hermes_sandbox.hetzner as hz
from hermes_sandbox.digitalocean import DigitalOceanProvider
from hermes_sandbox.digitalocean import _to_vm as do_to_vm
from hermes_sandbox.hetzner import HetznerProvider
from hermes_sandbox.hetzner import _to_vm as hz_to_vm

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
    "labels": {"hermes-sandbox": "true"},
    "server_type": {"name": "cx22"},
    "image": {"name": "ubuntu-24.04"},
}


def test_hetzner_to_vm_parses_ipv4_and_ipv6():
    vm = hz_to_vm(HZ_SERVER)
    assert vm.public_ip == "1.2.3.4", f"ipv4 should be parsed from public_net, got {vm.public_ip!r}"
    assert vm.public_ipv6 == "2a01:db8::", (
        f"ipv6 /64 prefix should be stripped, got {vm.public_ipv6!r}"
    )
    assert vm.is_running(), f"status 'running' should satisfy is_running(), status={vm.status!r}"
    assert vm.region == "nbg1", (
        f"region should come from datacenter.location.name, got {vm.region!r}"
    )


def test_hetzner_provision_polls_for_ip():
    calls = {"n": 0}

    def fake_request(method, path, token, body=None, timeout=30):
        if method == "GET" and path.startswith("/ssh_keys"):
            return {"ssh_keys": []}
        if method == "POST" and path == "/ssh_keys":
            return {"ssh_key": {"name": "hermes-sandbox"}}
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
    assert vm.public_ip == "1.2.3.4", (
        f"provision should poll until public_ip is set, got {vm.public_ip!r}"
    )
    assert calls["n"] >= 2, (
        f"provision should poll GET /servers/<id> at least twice, got {calls['n']}"
    )


def test_hetzner_teardown_404_is_success():
    from hermes_sandbox.models import ApiError

    def fake_request(method, path, token, body=None, timeout=30):
        raise ApiError("GET", path, status_code=404, detail="not found")

    p = HetznerProvider(token="tok", request_fn=fake_request)
    p.teardown("1")  # should not raise


def test_hetzner_teardown_500_raises_api_error():
    from hermes_sandbox.models import ApiError

    def fake_request(method, path, token, body=None, timeout=30):
        raise ApiError("DELETE", path, status_code=500, detail="boom")

    p = HetznerProvider(token="tok", request_fn=fake_request)
    with pytest.raises(ApiError) as exc_info:
        p.teardown("1")
    assert exc_info.value.status_code == 500, (
        f"teardown should propagate non-404 ApiError, got status {exc_info.value.status_code}"
    )


def test_api_error_is_a_runtime_error_subclass():
    """ApiError subclasses RuntimeError so existing 'except RuntimeError' callers
    that don't check status_code still catch it (backwards-compatible).
    """
    from hermes_sandbox.models import ApiError

    assert issubclass(ApiError, RuntimeError), (
        "ApiError must subclass RuntimeError for backwards compatibility"
    )


def test_hetzner_teardown_other_error_raises():
    def fake_request(method, path, token, body=None, timeout=30):
        raise RuntimeError("HTTP 500 boom")

    p = HetznerProvider(token="tok", request_fn=fake_request)
    with pytest.raises(RuntimeError):
        p.teardown("1")


def test_hetzner_requires_token():
    with pytest.raises(ValueError):
        HetznerProvider(token="")


def test_hetzner_list_by_label_url_encodes_selector():
    """label_selector value must be percent-encoded so '=' becomes '%3D'.

    A raw '=' inside the selector value is a reserved query-string character;
    Hetzner will mis-parse ``...?label_selector=hermes-sandbox=true`` because
    the second '=' looks like a new parameter boundary.
    """
    seen: list[str] = []

    def fake_request(method, path, token, body=None, timeout=30):
        seen.append(path)
        return {"servers": []}

    p = HetznerProvider(token="tok", request_fn=fake_request)
    p.list_by_label("hermes-sandbox", "true")
    assert seen, "list_by_label should issue at least one GET request"
    path = seen[-1]
    assert path == "/servers?label_selector=hermes-sandbox%3Dtrue", (
        f"label_selector '=' must be percent-encoded as %3D; got {path!r}"
    )


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
    "tags": ["hermes-sandbox"],
    "size": {"slug": "s-2vcpu-4gb"},
    "image": {"slug": "ubuntu-24-04-x64"},
}


def test_do_to_vm_parses_ipv4_ipv6_and_normalises_active():
    vm = do_to_vm(DO_DROPLET)
    assert vm.public_ip == "5.6.7.8", (
        f"ipv4 should be parsed from networks.v4, got {vm.public_ip!r}"
    )
    assert vm.public_ipv6 == "2604::1", (
        f"ipv6 should be parsed from networks.v6, got {vm.public_ipv6!r}"
    )
    assert vm.is_running(), f"'active' should satisfy is_running(), status={vm.status!r}"


def test_do_provision_polls_until_active():
    calls = {"n": 0}

    def fake_request(method, path, token, body=None, timeout=30):
        if method == "GET" and path == "/account/keys":
            return {"ssh_keys": []}
        if method == "POST" and path == "/account/keys":
            return {"ssh_key": {"id": 7}}
        if method == "POST" and path == "/droplets":
            return {"droplet": {**DO_DROPLET, "status": "new", "networks": {"v4": [], "v6": []}}}
        if method == "GET" and path.startswith("/droplets/"):
            calls["n"] += 1
            if calls["n"] < 2:
                return {
                    "droplet": {**DO_DROPLET, "status": "new", "networks": {"v4": [], "v6": []}}
                }
            return {"droplet": DO_DROPLET}
        raise AssertionError(f"unexpected {method} {path}")

    p = DigitalOceanProvider(token="tok", poll_interval=0, poll_timeout=5, request_fn=fake_request)
    vm = p.provision("n", "s-2vcpu-4gb", "fra1", "ubuntu-24-04-x64", "ssh-ed25519 K")
    assert vm.public_ip == "5.6.7.8", (
        f"provision should poll until public_ip is set, got {vm.public_ip!r}"
    )
    assert vm.is_running(), f"provision should return a running VM, status={vm.status!r}"


def test_do_teardown_404_is_success():
    from hermes_sandbox.models import ApiError

    def fake_request(method, path, token, body=None, timeout=30):
        raise ApiError("DELETE", path, status_code=404, detail="not found")

    p = DigitalOceanProvider(token="tok", request_fn=fake_request)
    p.teardown("1")


def test_do_requires_token():
    with pytest.raises(ValueError):
        DigitalOceanProvider(token="")


def test_do_list_by_label_uses_label_key_as_tag():
    """list_by_label must query the tag named by label_key, not a hardcoded tag.

    DigitalOcean tags carry no value, so label_value is ignored — but the tag
    name comes from the caller.  The contract test only passes today because it
    happens to call with label_key == the hardcoded tag.
    """
    seen: list[str] = []

    def fake_request(method, path, token, body=None, timeout=30):
        seen.append(path)
        if path == "/droplets?tag_name=custom-tag":
            return {"droplets": [DO_DROPLET]}
        return {"droplets": []}

    p = DigitalOceanProvider(token="tok", request_fn=fake_request)
    found = p.list_by_label("custom-tag", "ignored-value")
    assert seen == ["/droplets?tag_name=custom-tag"], (
        f"list_by_label should query tag_name='custom-tag', got {seen!r}"
    )
    assert len(found) == 1, f"list_by_label should return one droplet, got {len(found)}"
    assert found[0].id == "99", f"droplet id should be '99', got {found[0].id!r}"


def test_do_provision_passes_ssh_key_id_as_int():
    """ssh_keys in the DO POST body must be a list of int IDs, not numeric strings.

    The DO API treats a numeric string ambiguously — it could be interpreted as
    a fingerprint (40-char hex), which produces a 404.  Passing an int makes the
    intent unambiguous.
    """
    captured: dict[str, object] = {}

    def fake_request(method, path, token, body=None, timeout=30):
        if method == "GET" and path == "/account/keys":
            return {"ssh_keys": []}
        if method == "POST" and path == "/account/keys":
            return {"ssh_key": {"id": 7}}
        if method == "POST" and path == "/droplets":
            captured["body"] = body
            return {"droplet": DO_DROPLET}
        if method == "GET" and path.startswith("/droplets/"):
            return {"droplet": DO_DROPLET}
        raise AssertionError(f"unexpected {method} {path}")

    p = DigitalOceanProvider(token="tok", poll_interval=0, poll_timeout=5, request_fn=fake_request)
    p.provision("n", "s-2vcpu-4gb", "fra1", "ubuntu-24-04-x64", "ssh-ed25519 K")
    body = captured["body"]
    assert isinstance(body, dict), f"POST body should be a dict, got {type(body).__name__}"
    ssh_keys = body["ssh_keys"]
    assert isinstance(ssh_keys, list), f"ssh_keys must be a list, got {type(ssh_keys).__name__}"
    assert isinstance(ssh_keys[0], int), (
        f"ssh_keys[0] must be an int (numeric string is ambiguous), got {type(ssh_keys[0]).__name__}: {ssh_keys[0]!r}"
    )


def test_do_ensure_ssh_key_returns_int_existing():
    """_ensure_ssh_key must return an int key id when the key already exists."""

    def fake_request(method, path, token, body=None, timeout=30):
        if method == "GET" and path == "/account/keys":
            return {"ssh_keys": [{"id": 7, "name": "hermes-sandbox"}]}
        raise AssertionError(f"unexpected {method} {path}")

    p = DigitalOceanProvider(token="tok", request_fn=fake_request)
    key_id = p._ensure_ssh_key("ssh-ed25519 K")
    assert isinstance(key_id, int), (
        f"_ensure_ssh_key must return int for existing key, got {type(key_id).__name__}: {key_id!r}"
    )
    assert key_id == 7, f"_ensure_ssh_key should return id 7, got {key_id!r}"


def test_do_ensure_ssh_key_returns_int_new():
    """_ensure_ssh_key must return an int key id when a new key is uploaded."""

    def fake_request(method, path, token, body=None, timeout=30):
        if method == "GET" and path == "/account/keys":
            return {"ssh_keys": []}
        if method == "POST" and path == "/account/keys":
            return {"ssh_key": {"id": 42}}
        raise AssertionError(f"unexpected {method} {path}")

    p = DigitalOceanProvider(token="tok", request_fn=fake_request)
    key_id = p._ensure_ssh_key("ssh-ed25519 K")
    assert isinstance(key_id, int), (
        f"_ensure_ssh_key must return int for new key, got {type(key_id).__name__}: {key_id!r}"
    )
    assert key_id == 42, f"_ensure_ssh_key should return id 42, got {key_id!r}"
