"""Tests for the agent-callable sandbox tools (ctx.register_tool handlers).

These tests exercise the handler functions directly — not just registration —
covering happy-path invocation, parameter validation, and error/edge cases.
The DryRunProvider is used so no real cloud API is hit.
"""

import json

import pytest

import hermes_sandbox
from hermes_sandbox.config import SandboxConfig
from hermes_sandbox.dry_run import DryRunProvider
from hermes_sandbox.lifecycle import VMLifecycleManager

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_manager(tmp_path, fake_key_factory):
    """Build a VMLifecycleManager backed by the DryRunProvider."""
    provider = DryRunProvider()
    config = SandboxConfig(provider_name="dry-run", token="", ssh_user="root")
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(exist_ok=True, parents=True)
    return VMLifecycleManager(
        provider=provider,
        config=config,
        hermes_home=hermes_home,
        key_factory=fake_key_factory,
        ssh_probe=lambda **kwargs: True,
    )


@pytest.fixture(autouse=True)
def _reset_manager(monkeypatch):
    """Ensure each test starts with a fresh module-level manager cache."""
    monkeypatch.setattr(hermes_sandbox, "_manager", None)
    monkeypatch.setattr(hermes_sandbox, "_sessions", {})
    yield


@pytest.fixture
def configured_manager(tmp_path, fake_key_factory, monkeypatch):
    """Inject a DryRunProvider-backed manager into the module-level singleton."""
    mgr = _make_manager(tmp_path, fake_key_factory)
    monkeypatch.setattr(hermes_sandbox, "_manager", mgr)
    # Also stub _invalidate_cached_terminal_env so handlers don't try to
    # reach into the real Hermes terminal tool's internals.
    monkeypatch.setattr(
        hermes_sandbox, "_invalidate_cached_terminal_env", lambda task_id="default": None
    )
    # Stub _apply_ssh_env so it doesn't pollute os.environ during tests.
    monkeypatch.setattr(hermes_sandbox, "_apply_ssh_env", lambda manager: None)
    return mgr


# ── Schema parameter tests ────────────────────────────────────────────────


class TestSandboxStatusSchema:
    """The sandbox_status tool takes no parameters — it's a read-only check."""

    def test_schema_has_empty_properties(self):
        schema = hermes_sandbox._SANDBOX_STATUS_SCHEMA
        assert schema["name"] == "sandbox_status"
        assert schema["parameters"]["properties"] == {}


class TestSandboxTeardownSchema:
    """sandbox_teardown should accept an optional vm_id to destroy a specific VM."""

    def test_schema_exposes_vm_id_parameter(self):
        schema = hermes_sandbox._SANDBOX_TEARDOWN_SCHEMA
        assert schema["name"] == "sandbox_teardown"
        props = schema["parameters"]["properties"]
        assert "vm_id" in props, "teardown schema must expose a vm_id parameter"

    def test_vm_id_is_optional(self):
        """vm_id should be optional so teardown without args destroys the recorded VM."""
        schema = hermes_sandbox._SANDBOX_TEARDOWN_SCHEMA
        required = schema["parameters"].get("required", [])
        assert "vm_id" not in required, "vm_id should be optional, not required"


class TestSandboxProvisionSchema:
    """sandbox_provision should accept optional configuration overrides."""

    def test_schema_exposes_instance_type_parameter(self):
        schema = hermes_sandbox._SANDBOX_PROVISION_SCHEMA
        props = schema["parameters"]["properties"]
        assert "instance_type" in props, "provision schema must expose instance_type"

    def test_schema_exposes_region_parameter(self):
        schema = hermes_sandbox._SANDBOX_PROVISION_SCHEMA
        props = schema["parameters"]["properties"]
        assert "region" in props, "provision schema must expose region"

    def test_all_provision_params_are_optional(self):
        """All provision parameters should be optional — provision with no args uses defaults."""
        schema = hermes_sandbox._SANDBOX_PROVISION_SCHEMA
        required = schema["parameters"].get("required", [])
        assert required == [], f"provision params should all be optional, got required={required!r}"


class TestSandboxCleanupSchema:
    """sandbox_cleanup should accept an optional confirm flag for safety."""

    def test_schema_exposes_confirm_parameter(self):
        schema = hermes_sandbox._SANDBOX_CLEANUP_SCHEMA
        props = schema["parameters"]["properties"]
        assert "confirm" in props, "cleanup schema must expose a confirm parameter for safety"

    def test_confirm_is_optional(self):
        schema = hermes_sandbox._SANDBOX_CLEANUP_SCHEMA
        required = schema["parameters"].get("required", [])
        assert "confirm" not in required


class TestSandboxListSchema:
    """sandbox_list takes no parameters — it lists all VMs on the provider."""

    def test_schema_has_empty_properties(self):
        schema = hermes_sandbox._SANDBOX_LIST_SCHEMA
        assert schema["name"] == "sandbox_list"
        assert schema["parameters"]["properties"] == {}


class TestSandboxRecreateSchema:
    """sandbox_recreate takes no parameters — it destroys + provisions in one step."""

    def test_schema_has_empty_properties(self):
        schema = hermes_sandbox._SANDBOX_RECREATE_SCHEMA
        assert schema["name"] == "sandbox_recreate"
        assert schema["parameters"]["properties"] == {}


# ── Handler happy-path tests ──────────────────────────────────────────────


class TestSandboxStatusHandler:
    """Happy-path: sandbox_status returns VM info when a VM exists."""

    def test_returns_no_vm_when_no_state(self, configured_manager):
        """When no VM is provisioned, status reports no_vm."""
        result = hermes_sandbox._sandbox_status_handler({})
        data = json.loads(result)
        assert data["status"] == "no_vm"
        assert "message" in data

    def test_returns_vm_info_after_provision(self, configured_manager):
        """After provisioning, status returns the VM's details."""
        configured_manager.ensure_vm()
        result = hermes_sandbox._sandbox_status_handler({})
        data = json.loads(result)
        assert data["status"] == "ok"
        assert "vm" in data
        assert "vm_id" in data["vm"]


class TestSandboxProvisionHandler:
    """Happy-path: sandbox_provision creates a VM and returns its details."""

    def test_provision_returns_vm_id_and_ip(self, configured_manager):
        result = hermes_sandbox._sandbox_provision_handler({})
        data = json.loads(result)
        assert data["status"] == "ok"
        assert "vm_id" in data
        assert "ip" in data
        assert "provider" in data

    def test_provision_is_idempotent(self, configured_manager):
        """Provisioning twice should reuse the same VM."""
        r1 = json.loads(hermes_sandbox._sandbox_provision_handler({}))
        r2 = json.loads(hermes_sandbox._sandbox_provision_handler({}))
        assert r1["vm_id"] == r2["vm_id"], "provision should reuse the existing VM"


class TestSandboxTeardownHandler:
    """Happy-path: sandbox_teardown destroys the current VM."""

    def test_teardown_returns_ok_after_provision(self, configured_manager):
        configured_manager.ensure_vm()
        result = hermes_sandbox._sandbox_teardown_handler({})
        data = json.loads(result)
        assert data["status"] == "ok"

    def test_teardown_returns_no_vm_when_empty(self, configured_manager):
        """Tearing down with no VM should report no_vm, not an error."""
        result = hermes_sandbox._sandbox_teardown_handler({})
        data = json.loads(result)
        assert data["status"] == "no_vm"

    def test_teardown_with_vm_id_destroys_specific_vm(self, configured_manager):
        """Passing vm_id should destroy that specific VM, not just the recorded one."""
        # Provision a VM
        vm1 = configured_manager.ensure_vm()
        vm1_id = vm1.id
        # The teardown handler with vm_id should target a specific VM
        result = hermes_sandbox._sandbox_teardown_handler({"vm_id": vm1_id})
        data = json.loads(result)
        assert data["status"] == "ok"


class TestSandboxRecreateHandler:
    """Happy-path: sandbox_recreate destroys + provisions in one step."""

    def test_recreate_returns_new_vm(self, configured_manager):
        configured_manager.ensure_vm()
        result = hermes_sandbox._sandbox_recreate_handler({})
        data = json.loads(result)
        assert data["status"] == "ok"
        assert "vm_id" in data
        assert "ip" in data

    def test_recreate_produces_different_vm_id(self, configured_manager):
        """Recreate should provision a fresh VM with a different ID."""
        vm1 = configured_manager.ensure_vm()
        result = json.loads(hermes_sandbox._sandbox_recreate_handler({}))
        assert result["vm_id"] != vm1.id, "recreate must produce a different VM"


class TestSandboxListHandler:
    """Happy-path: sandbox_list returns all sandbox VMs."""

    def test_list_returns_empty_when_no_vms(self, configured_manager):
        result = hermes_sandbox._sandbox_list_handler({})
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["vms"] == []

    def test_list_returns_vms_after_provision(self, configured_manager):
        configured_manager.ensure_vm()
        result = hermes_sandbox._sandbox_list_handler({})
        data = json.loads(result)
        assert data["status"] == "ok"
        assert len(data["vms"]) == 1
        assert "id" in data["vms"][0]
        assert "name" in data["vms"][0]
        assert "provider" in data["vms"][0]


class TestSandboxCleanupHandler:
    """Happy-path: sandbox_cleanup destroys stray VMs."""

    def test_cleanup_returns_empty_when_no_strays(self, configured_manager):
        """With only the known VM, cleanup should report nothing destroyed."""
        configured_manager.ensure_vm()
        result = hermes_sandbox._sandbox_cleanup_handler({"confirm": True})
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["destroyed"] == []

    def test_cleanup_destroys_stray_vms(self, configured_manager):
        """Stray VMs (not in known set) should be destroyed when confirmed."""
        # Provision a stray VM directly through the provider
        provider = configured_manager.provider
        kp = configured_manager.keypair()
        stray = provider.provision(
            name="stray-vm",
            instance_type="cx22",
            region="fsn1",
            image="ubuntu-2204",
            ssh_public_key=kp.public_key,
            labels={"hermes-sandbox": "true"},
        )
        # The known VM is the one in state.json; the stray is not tracked
        configured_manager.ensure_vm()

        result = hermes_sandbox._sandbox_cleanup_handler({"confirm": True})
        data = json.loads(result)
        assert data["status"] == "ok"
        assert stray.id in data["destroyed"], "stray VM should be in destroyed list"

    def test_cleanup_without_confirm_does_not_destroy(self, configured_manager):
        """Without confirm=True, cleanup should refuse to destroy VMs."""
        # Create a stray VM
        provider = configured_manager.provider
        kp = configured_manager.keypair()
        stray = provider.provision(
            name="stray-vm",
            instance_type="cx22",
            region="fsn1",
            image="ubuntu-2204",
            ssh_public_key=kp.public_key,
            labels={"hermes-sandbox": "true"},
        )
        configured_manager.ensure_vm()

        # Without confirm, cleanup should list strays but not destroy
        result = hermes_sandbox._sandbox_cleanup_handler({})
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["destroyed"] == [], "cleanup without confirm should not destroy"
        assert stray.id in data["would_destroy"], "stray should appear in would_destroy list"


# ── Error / edge-case tests ────────────────────────────────────────────────


class TestNoManagerError:
    """When the sandbox is not configured, handlers should return error JSON, not crash."""

    def test_status_returns_error_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(hermes_sandbox, "_manager", None)
        monkeypatch.setattr(hermes_sandbox, "_build_manager", lambda: None)
        result = hermes_sandbox._sandbox_status_handler({})
        data = json.loads(result)
        assert "error" in data

    def test_provision_returns_error_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(hermes_sandbox, "_manager", None)
        monkeypatch.setattr(hermes_sandbox, "_build_manager", lambda: None)
        result = hermes_sandbox._sandbox_provision_handler({})
        data = json.loads(result)
        assert "error" in data

    def test_teardown_returns_error_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(hermes_sandbox, "_manager", None)
        monkeypatch.setattr(hermes_sandbox, "_build_manager", lambda: None)
        result = hermes_sandbox._sandbox_teardown_handler({})
        data = json.loads(result)
        assert "error" in data

    def test_recreate_returns_error_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(hermes_sandbox, "_manager", None)
        monkeypatch.setattr(hermes_sandbox, "_build_manager", lambda: None)
        result = hermes_sandbox._sandbox_recreate_handler({})
        data = json.loads(result)
        assert "error" in data

    def test_list_returns_error_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(hermes_sandbox, "_manager", None)
        monkeypatch.setattr(hermes_sandbox, "_build_manager", lambda: None)
        result = hermes_sandbox._sandbox_list_handler({})
        data = json.loads(result)
        assert "error" in data

    def test_cleanup_returns_error_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(hermes_sandbox, "_manager", None)
        monkeypatch.setattr(hermes_sandbox, "_build_manager", lambda: None)
        result = hermes_sandbox._sandbox_cleanup_handler({})
        data = json.loads(result)
        assert "error" in data


class TestHandlerExceptionHandling:
    """Handlers must catch exceptions and return error JSON, never raise."""

    def test_provision_catches_exception(self, configured_manager, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("cloud API down")

        monkeypatch.setattr(configured_manager, "ensure_vm", boom)
        result = hermes_sandbox._sandbox_provision_handler({})
        data = json.loads(result)
        assert "error" in data
        assert "cloud API down" in data["error"]

    def test_teardown_catches_exception(self, configured_manager, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("teardown failed")

        monkeypatch.setattr(configured_manager, "teardown", boom)
        result = hermes_sandbox._sandbox_teardown_handler({})
        data = json.loads(result)
        assert "error" in data
        assert "teardown failed" in data["error"]

    def test_recreate_catches_exception(self, configured_manager, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("recreate failed")

        monkeypatch.setattr(configured_manager, "recreate", boom)
        result = hermes_sandbox._sandbox_recreate_handler({})
        data = json.loads(result)
        assert "error" in data
        assert "recreate failed" in data["error"]

    def test_list_catches_exception(self, configured_manager, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("list failed")

        monkeypatch.setattr(configured_manager, "list_vms", boom)
        result = hermes_sandbox._sandbox_list_handler({})
        data = json.loads(result)
        assert "error" in data
        assert "list failed" in data["error"]

    def test_cleanup_catches_exception(self, configured_manager, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("cleanup failed")

        monkeypatch.setattr(configured_manager, "cleanup_strays", boom)
        result = hermes_sandbox._sandbox_cleanup_handler({"confirm": True})
        data = json.loads(result)
        assert "error" in data
        assert "cleanup failed" in data["error"]


class TestHandlerReturnsValidJson:
    """Every handler must return a valid JSON string."""

    @pytest.mark.parametrize(
        "handler_name",
        [
            "_sandbox_status_handler",
            "_sandbox_provision_handler",
            "_sandbox_teardown_handler",
            "_sandbox_recreate_handler",
            "_sandbox_list_handler",
            "_sandbox_cleanup_handler",
        ],
    )
    def test_handler_returns_parseable_json(self, configured_manager, handler_name):
        handler = getattr(hermes_sandbox, handler_name)
        result = handler({"confirm": True} if "cleanup" in handler_name else {})
        assert isinstance(result, str), f"{handler_name} must return a string"
        data = json.loads(result)
        assert isinstance(data, dict), f"{handler_name} must return a JSON object"
