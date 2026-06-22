"""Tests for the VM lifecycle manager (reuse, teardown, state, IPv6)."""

import json
import time

import pytest

from hermes_sandbox.config import SandboxConfig
from hermes_sandbox.dry_run import DryRunProvider
from hermes_sandbox.lifecycle import VMLifecycleManager


def make_manager(tmp_path, provider=None, config=None, fake_key_factory=None, clock=None):
    provider = provider or DryRunProvider()
    config = config or SandboxConfig(provider_name="dry-run", token="", ssh_user="root")
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(exist_ok=True)
    return VMLifecycleManager(
        provider=provider,
        config=config,
        hermes_home=hermes_home,
        key_factory=fake_key_factory,
        clock=clock or time.time,
    )


def test_config_from_env_hetzner(monkeypatch):
    monkeypatch.setenv("SANDBOX_PROVIDER", "hetzner")
    monkeypatch.setenv("HCLOUD_TOKEN", "tok")
    monkeypatch.setenv("SANDBOX_INSTANCE_TYPE", "cpx21")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.provider_name == "hetzner", f"expected hetzner, got {cfg.provider_name!r}"
    assert cfg.token == "tok", f"expected tok, got {cfg.token!r}"
    assert cfg.instance_type == "cpx21", f"expected cpx21, got {cfg.instance_type!r}"
    assert cfg.is_configured(), "hetzner with a token must be configured"


def test_config_from_env_hetzner_defaults(monkeypatch):
    """Hetzner's own defaults apply when no SANDBOX_* override is set."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "hetzner")
    monkeypatch.setenv("HCLOUD_TOKEN", "tok")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.region == "nbg1", f"expected nbg1, got {cfg.region!r}"
    assert cfg.image == "ubuntu-24.04", f"expected ubuntu-24.04, got {cfg.image!r}"
    assert cfg.instance_type == "cx23", f"expected cx23, got {cfg.instance_type!r}"


def test_config_from_env_digitalocean_defaults(monkeypatch):
    monkeypatch.setenv("SANDBOX_PROVIDER", "digitalocean")
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dotok")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.provider_name == "digitalocean", f"expected digitalocean, got {cfg.provider_name!r}"
    assert cfg.token == "dotok", f"expected dotok, got {cfg.token!r}"
    assert cfg.region == "fra1", f"expected fra1, got {cfg.region!r}"
    assert cfg.image == "ubuntu-24-04-x64", f"expected ubuntu-24-04-x64, got {cfg.image!r}"
    assert cfg.instance_type == "s-2vcpu-4gb", f"expected s-2vcpu-4gb, got {cfg.instance_type!r}"


def test_config_from_env_mode_defaults_to_shared(monkeypatch):
    """Without SANDBOX_MODE, the default is shared (one VM for all sessions)."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "hetzner")
    monkeypatch.setenv("HCLOUD_TOKEN", "tok")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.mode == "shared", f"expected shared, got {cfg.mode!r}"


def test_config_from_env_mode_isolated(monkeypatch):
    """SANDBOX_MODE=isolated gives each session its own VM."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "hetzner")
    monkeypatch.setenv("HCLOUD_TOKEN", "tok")
    monkeypatch.setenv("SANDBOX_MODE", "isolated")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.mode == "isolated", f"expected isolated, got {cfg.mode!r}"


def test_config_not_configured_without_token():
    cfg = SandboxConfig(provider_name="hetzner", token="")
    assert not cfg.is_configured(), "hetzner without a token must not be configured"


def test_config_dry_run_is_configured_without_token():
    assert SandboxConfig(provider_name="dry-run", token="").is_configured(), (
        "dry-run must be configured even without a token"
    )


def test_ensure_vm_provisions_and_records_state(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    vm = mgr.ensure_vm()
    assert vm.is_running(), f"provisioned VM must be running, status={vm.status!r}"
    state = json.loads(mgr.state_path.read_text())
    assert state["vm_id"] == vm.id, f"state.vm_id must match VM id, got {state['vm_id']!r}"
    assert state["ssh_host"] == vm.preferred_ip(), (
        f"state.ssh_host must be the VM's preferred IP, got {state['ssh_host']!r}"
    )
    assert state["ssh_user"] == "root", f"expected root, got {state['ssh_user']!r}"


def test_ensure_vm_reuses_existing(tmp_path, fake_key_factory):
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    first = mgr.ensure_vm()
    second = mgr.ensure_vm()
    assert first.id == second.id, (
        f"second ensure_vm must reuse the first VM, got {first.id!r} then {second.id!r}"
    )
    provisions = [c for c in provider.calls if c["op"] == "provision"]
    assert len(provisions) == 1, (
        f"provision must fire exactly once on reuse, fired {len(provisions)} times"
    )


def test_ensure_vm_reprovisions_when_dead(tmp_path, fake_key_factory):
    import dataclasses

    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    first = mgr.ensure_vm()
    provider._vms[first.id] = dataclasses.replace(provider._vms[first.id], status="off")
    second = mgr.ensure_vm()
    assert second.id != first.id, (
        f"a dead VM must be replaced with a fresh one, both were {first.id!r}"
    )
    provisions = [c for c in provider.calls if c["op"] == "provision"]
    assert len(provisions) == 2, (
        f"reprovisioning a dead VM must provision twice, got {len(provisions)}"
    )


def test_ensure_vm_reprovisions_when_provider_forgets(tmp_path, fake_key_factory):
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    first = mgr.ensure_vm()
    del provider._vms[first.id]
    second = mgr.ensure_vm()
    assert second.id != first.id, (
        f"a vanished VM must be replaced with a fresh one, both were {first.id!r}"
    )


def test_teardown_clears_state(tmp_path, fake_key_factory):
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    assert mgr.state_path.exists(), "state file must exist after provisioning"
    assert mgr.teardown() is True, "teardown of a live VM must return True"
    assert not mgr.state_path.exists(), "state file must be removed after teardown"
    assert mgr.teardown() is False, "teardown with no state must return False (idempotent)"


def test_maybe_auto_teardown_respects_config(tmp_path, fake_key_factory):
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    assert mgr.maybe_auto_teardown() is False, "auto_teardown_seconds=0 must never tear down"
    assert mgr.state_path.exists(), "state must survive when auto-teardown is disabled"

    provider2 = DryRunProvider()
    cfg = SandboxConfig(
        provider_name="dry-run", token="", ssh_user="root", auto_teardown_seconds=60
    )
    mgr2 = make_manager(tmp_path, provider=provider2, config=cfg, fake_key_factory=fake_key_factory)
    mgr2.ensure_vm()
    assert mgr2.maybe_auto_teardown() is True, (
        "auto_teardown_seconds>0 must tear down on session end"
    )
    assert not mgr2.state_path.exists(), "state must be gone after auto-teardown"


def test_state_records_ipv6_and_ssh_host(tmp_path, fake_key_factory):
    import dataclasses

    provider = DryRunProvider()
    orig_provision = provider.provision

    def provision_with_v6(*a, **k):
        vm = orig_provision(*a, **k)
        return dataclasses.replace(vm, public_ipv6="2a01:db8::1")

    provider.provision = provision_with_v6
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    state = json.loads(mgr.state_path.read_text())
    assert state["public_ipv6"] == "2a01:db8::1", (
        f"state must record the VM's IPv6, got {state['public_ipv6']!r}"
    )
    assert state["ssh_host"] == "2a01:db8::1", (
        f"ssh_host must prefer IPv6, got {state['ssh_host']!r}"
    )


def test_save_state_restricts_file_permissions(tmp_path, fake_key_factory):
    """The state file holds connection details — restrict it to the owner (0600)."""
    import os
    import stat

    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    mode = stat.S_IMODE(os.stat(mgr.state_path).st_mode)
    assert mode == 0o600, f"state file must be 0600, got {mode:#o}"


def test_status_returns_none_without_state(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    assert mgr.status() is None, "status() must be None when no state is recorded"


def test_load_state_returns_none_on_corrupt_json(tmp_path):
    from hermes_sandbox.state import JsonStateRepository

    repo = JsonStateRepository(tmp_path)
    repo.path.write_text("{not valid json")
    assert repo.load() is None, "corrupt JSON must yield None, not raise"


def test_load_state_returns_none_on_non_dict_json(tmp_path):
    from hermes_sandbox.state import JsonStateRepository

    repo = JsonStateRepository(tmp_path)
    repo.path.write_text("[1, 2, 3]")
    assert repo.load() is None, "a non-dict JSON payload must yield None"


def test_load_state_returns_none_on_unreadable_path(tmp_path):
    from hermes_sandbox.state import JsonStateRepository

    repo = JsonStateRepository(tmp_path)
    repo._path = tmp_path  # type: ignore[attr-defined]
    assert repo.load() is None, "an unreadable path must yield None, not raise"


def test_state_repository_roundtrip(tmp_path):
    """save() then load() returns an equal SandboxState (typed persistence)."""
    from hermes_sandbox.state import JsonStateRepository, SandboxState

    repo = JsonStateRepository(tmp_path)
    assert repo.load() is None, "a fresh repo must have no state"

    state = SandboxState(
        vm_id="1",
        provider="dry-run",
        name="n",
        region="r",
        public_ip="1.2.3.4",
        public_ipv6="",
        ssh_host="1.2.3.4",
        ssh_user="root",
        created_at=1.0,
        last_used=2.0,
    )
    repo.save(state)
    loaded = repo.load()
    assert loaded == state, f"round-tripped state must equal the original, got {loaded!r}"
    repo.clear()
    assert repo.load() is None, "load() must be None after clear()"


def test_manager_accepts_injected_state_repo(tmp_path, fake_key_factory):
    """The manager depends on the StateRepository Protocol, not the filesystem:
    an in-memory repo lets the use-case run without any file I/O."""
    from hermes_sandbox.state import SandboxState

    class InMemoryRepo:
        def __init__(self) -> None:
            self._state: SandboxState | None = None
            self.path = tmp_path / "mem"
            self.key_dir = tmp_path

        def load(self) -> SandboxState | None:
            return self._state

        def save(self, state: SandboxState) -> None:
            self._state = state

        def clear(self) -> None:
            self._state = None

    provider = DryRunProvider()
    config = SandboxConfig(provider_name="dry-run", token="", ssh_user="root")
    mgr = VMLifecycleManager(
        provider=provider,
        config=config,
        hermes_home=tmp_path / ".hermes",
        key_factory=fake_key_factory,
        state_repo=InMemoryRepo(),
    )
    vm = mgr.ensure_vm()
    assert vm.is_running(), f"provisioned VM must be running, status={vm.status!r}"
    assert not (tmp_path / ".hermes" / "sandbox").exists(), (
        "no state.json file must be created with an in-memory repo"
    )
    assert mgr.ssh_env() is not None, "repo-backed state must produce a valid ssh_env()"


def test_ssh_env_returns_mapping_when_provisioned(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    env = mgr.ssh_env()
    assert env is not None, "ssh_env() must return a mapping once a VM is provisioned"
    assert env["TERMINAL_ENV"] == "ssh", f"expected TERMINAL_ENV=ssh, got {env['TERMINAL_ENV']!r}"
    assert env["TERMINAL_SSH_HOST"], "TERMINAL_SSH_HOST must be set to the VM's IP"
    assert env["TERMINAL_SSH_USER"] == "root", (
        f"expected TERMINAL_SSH_USER=root, got {env['TERMINAL_SSH_USER']!r}"
    )
    assert env["TERMINAL_SSH_KEY"].endswith("hermes-sandbox"), (
        f"TERMINAL_SSH_KEY must end with hermes-sandbox, got {env['TERMINAL_SSH_KEY']!r}"
    )


def test_ssh_env_returns_none_without_state(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    assert mgr.ssh_env() is None, "ssh_env() must be None when no state is recorded"


def test_status_returns_sandboxstatus_value_object(tmp_path, fake_key_factory):
    from hermes_sandbox.lifecycle import SandboxStatus

    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    st = mgr.status()
    assert isinstance(st, SandboxStatus), f"status() must return a SandboxStatus, got {type(st)!r}"
    assert st.vm_id, "status must carry a non-empty vm_id"
    assert st.provider == "dry-run", f"expected provider dry-run, got {st.provider!r}"
    assert st.public_ip, "status must carry a non-empty public_ip"
    assert st.alive is True, f"freshly provisioned VM must be alive, got alive={st.alive!r}"
    assert st.live_status == "running", f"expected live_status running, got {st.live_status!r}"
    d = st.to_dict()
    assert d["vm_id"] == st.vm_id, "to_dict must preserve vm_id"
    assert d["provider"] == "dry-run", "to_dict must preserve provider"
    assert d["public_ip"] == st.public_ip, "to_dict must preserve public_ip"


def test_status_marks_dead_when_provider_forgets(tmp_path, fake_key_factory):
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    del provider._vms[mgr.status().vm_id]  # type: ignore[union-attr]
    st = mgr.status()
    assert st is not None, "status() must still return recorded state even when the VM vanished"
    assert st.alive is False, f"a vanished VM must be reported dead, got alive={st.alive!r}"
    assert st.error, "status must record the provider error when the probe fails"


def test_list_vms_returns_provider_results(tmp_path, fake_key_factory):
    """list_vms() delegates to provider.list_by_label with the sandbox label."""
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    vms = mgr.list_vms()
    assert len(vms) == 1, f"expected exactly one VM, got {len(vms)}"
    assert vms[0].provider == "dry-run", f"expected provider dry-run, got {vms[0].provider!r}"
    assert vms[0].is_running(), f"listed VM must be running, status={vms[0].status!r}"


def test_list_vms_empty_when_no_vms(tmp_path, fake_key_factory):
    """list_vms() returns an empty list when no VMs exist on the cloud."""
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    assert mgr.list_vms() == [], f"expected empty list, got {mgr.list_vms()!r}"


def test_recreate_tears_down_old_and_provisions_new(tmp_path, fake_key_factory):
    """recreate() destroys the current VM and provisions a fresh one."""
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    first = mgr.ensure_vm()
    second = mgr.recreate()
    assert second.id != first.id, (
        f"recreate must provision a new VM with a different UUID, both were {first.id!r}"
    )
    teardowns = [c for c in provider.calls if c["op"] == "teardown"]
    assert len(teardowns) == 1, f"recreate must tear down exactly once, got {len(teardowns)}"
    assert teardowns[0]["vm_id"] == first.id, (
        f"recreate must tear down the old VM ({first.id!r}), got {teardowns[0]['vm_id']!r}"
    )


def test_recreate_when_no_vm_just_provisions(tmp_path, fake_key_factory):
    """recreate() with no existing state just provisions."""
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    vm = mgr.recreate()
    assert vm.is_running(), f"recreated VM must be running, status={vm.status!r}"
    assert vm.id, f"recreated VM must have a non-empty UUID, got {vm.id!r}"


def test_cleanup_strays_destroys_unreferenced_vms(tmp_path, fake_key_factory):
    """cleanup_strays tears down cloud VMs not in the known set."""
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    vm = mgr.ensure_vm()
    destroyed = mgr.cleanup_strays({vm.id})
    assert destroyed == [], f"known VM must not be torn down, got {destroyed!r}"
    assert provider.get(vm.id), "known VM must still exist after cleanup"

    destroyed = mgr.cleanup_strays(set())
    assert vm.id in destroyed, f"stray VM must be torn down, destroyed={destroyed!r}"
    with pytest.raises(KeyError):
        provider.get(vm.id)


def test_cleanup_strays_when_no_vms(tmp_path, fake_key_factory):
    """cleanup_strays returns empty list when no VMs exist on the cloud."""
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    assert mgr.cleanup_strays(set()) == [], "cleanup with no cloud VMs must return []"


def test_cleanup_strays_preserves_known_and_destroys_unknown(tmp_path, fake_key_factory):
    """cleanup_strays keeps VMs in the known set and destroys the rest."""
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    vm1 = provider.provision("v1", "t", "r", "i", "k", labels={"hermes-sandbox": "true"})
    vm2 = provider.provision("v2", "t", "r", "i", "k", labels={"hermes-sandbox": "true"})
    destroyed = mgr.cleanup_strays({vm1.id})
    assert vm2.id in destroyed, f"stray vm2 must be destroyed, got {destroyed!r}"
    assert vm1.id not in destroyed, f"known vm1 must survive, got {destroyed!r}"
    assert provider.get(vm1.id), "known vm1 must still exist after cleanup"
    with pytest.raises(KeyError):
        provider.get(vm2.id)


def test_sandboxstatus_is_frozen():
    """SandboxStatus is an immutable value object."""
    import dataclasses

    from hermes_sandbox.lifecycle import SandboxStatus

    st = SandboxStatus(
        vm_id="1",
        provider="dry-run",
        name="n",
        region="r",
        public_ip="1.2.3.4",
        public_ipv6="",
        ssh_host="1.2.3.4",
        ssh_user="root",
        created_at=0.0,
        last_used=0.0,
        alive=True,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        st.alive = False  # type: ignore[misc]


# ── Coverage gap-fillers: error paths and edge cases ──────────────────────


def test_teardown_clears_state_even_when_provider_raises(tmp_path, fake_key_factory):
    """A provider teardown error must still clear local state (no dangling pointer)."""
    provider = DryRunProvider()

    def explode(_vm_id: str) -> None:
        raise RuntimeError("cloud API is down")

    provider.teardown = explode  # type: ignore[assignment]
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    assert mgr.state_path.exists(), "state must exist before teardown"
    result = mgr.teardown()
    assert result is True, "teardown must report success even when the provider errors"
    assert not mgr.state_path.exists(), "state must be cleared regardless of provider error"


def test_cleanup_strays_continues_when_one_teardown_fails(tmp_path, fake_key_factory):
    """cleanup_strays must log and skip a VM whose teardown raises, not abort."""
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    vm1 = provider.provision("v1", "t", "r", "i", "k", labels={"hermes-sandbox": "true"})
    vm2 = provider.provision("v2", "t", "r", "i", "k", labels={"hermes-sandbox": "true"})

    call_count = {"n": 0}
    real_teardown = provider.teardown

    def flaky_teardown(vm_id: str) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient failure")
        real_teardown(vm_id)

    provider.teardown = flaky_teardown  # type: ignore[assignment]
    destroyed = mgr.cleanup_strays(set())
    assert len(destroyed) == 1, (
        f"only the non-failing VM should be reported destroyed, got {destroyed!r}"
    )
    assert destroyed[0] in {vm1.id, vm2.id}, f"destroyed must be one of the VMs, got {destroyed!r}"


def test_ssh_env_returns_none_when_host_is_empty(tmp_path, fake_key_factory):
    """ssh_env() must return None when the recorded state has no resolvable host."""
    from hermes_sandbox.state import SandboxState

    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    mgr._repo.save(
        SandboxState(
            vm_id="1",
            provider="dry-run",
            name="n",
            region="r",
            public_ip="",
            public_ipv6="",
            ssh_host="",
            ssh_user="root",
            created_at=0.0,
            last_used=0.0,
        )
    )
    assert mgr.ssh_env() is None, "ssh_env must be None when no host is resolvable"


def test_state_dir_property_matches_repo_key_dir(tmp_path, fake_key_factory):
    """The state_dir back-compat accessor must mirror the repo's key_dir."""
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    assert mgr.state_dir == mgr._repo.key_dir, (
        "state_dir property must equal the repository's key_dir"
    )


def test_load_state_returns_none_on_missing_required_field(tmp_path):
    """A JSON dict missing a required SandboxState field must yield None."""
    from hermes_sandbox.state import JsonStateRepository

    repo = JsonStateRepository(tmp_path)
    repo.path.write_text('{"vm_id": "1"}')
    assert repo.load() is None, "partial JSON missing required fields must yield None"


def test_ssh_env_includes_port_when_not_default(tmp_path, fake_key_factory):
    """ssh_env() must set TERMINAL_SSH_PORT when the VM uses a non-22 port."""
    from hermes_sandbox.state import SandboxState

    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr._repo.save(
        SandboxState(
            vm_id="1",
            provider="dry-run",
            name="n",
            region="r",
            public_ip="1.2.3.4",
            public_ipv6="",
            ssh_host="1.2.3.4",
            ssh_user="root",
            created_at=0.0,
            last_used=0.0,
            ssh_port=2222,
        )
    )
    env = mgr.ssh_env()
    assert env is not None, "ssh_env must return a mapping with a valid host"
    assert env.get("TERMINAL_SSH_PORT") == "2222", (
        f"non-default port must appear in env, got {env.get('TERMINAL_SSH_PORT')!r}"
    )


def test_config_from_env_unregistered_provider_falls_back_to_defaults(monkeypatch):
    """An unregistered provider name must fall back to base CloudProvider defaults."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "totally-fake-provider")
    monkeypatch.setenv("SANDBOX_TOKEN", "tok")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.region == "nbg1", (
        f"unregistered provider must fall back to base default region, got {cfg.region!r}"
    )
    assert cfg.image == "ubuntu-24.04", (
        f"unregistered provider must fall back to base default image, got {cfg.image!r}"
    )
    assert cfg.token == "tok", (
        f"unregistered provider must fall back to SANDBOX_TOKEN, got {cfg.token!r}"
    )
