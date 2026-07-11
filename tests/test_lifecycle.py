"""Tests for the VM lifecycle manager (reuse, teardown, state, IPv6)."""

import json
import time

import pytest

from hermes_sandbox.config import SandboxConfig
from hermes_sandbox.dry_run import DryRunProvider
from hermes_sandbox.lifecycle import VMLifecycleManager


def make_manager(tmp_path, provider=None, config=None, fake_key_factory=None, clock=None, ssh_probe=None):
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
        ssh_probe=ssh_probe or (lambda **kwargs: True),
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
    assert state["ssh_host"] == state["public_ip"], (
        f"ssh_host must prefer IPv4, got {state['ssh_host']!r}"
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
        ssh_probe=lambda **kwargs: True,
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


# ── reap_stale (TTL-based VM reaping) ─────────────────────────────────────


def test_reap_stale_disabled_when_ttl_zero(tmp_path, fake_key_factory):
    """TTL of 0 means reaping is disabled — VM must survive."""
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    assert mgr.reap_stale(0) is False, "ttl_seconds=0 must never reap"
    assert mgr.state_path.exists(), "state must survive when TTL is disabled"


def test_reap_stale_noops_when_no_state(tmp_path, fake_key_factory):
    """With no VM recorded, reap is a no-op."""
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    assert mgr.reap_stale(3600) is False, "reap with no state must return False"


def test_reap_stale_keeps_fresh_vm(tmp_path, fake_key_factory):
    """A VM used within the TTL window must survive."""
    now = {"t": 1000.0}
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory, clock=lambda: now["t"])
    mgr.ensure_vm()
    # Advance only 60s — well within a 3600s TTL.
    now["t"] += 60
    assert mgr.reap_stale(3600) is False, "a fresh VM must not be reaped"
    assert mgr.state_path.exists(), "state must survive for a fresh VM"


def test_reap_stale_tears_down_stale_vm(tmp_path, fake_key_factory):
    """A VM unused longer than the TTL must be torn down."""
    now = {"t": 1000.0}
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory, clock=lambda: now["t"])
    mgr.ensure_vm()
    # Advance 2 hours — past a 1-hour TTL.
    now["t"] += 7200
    assert mgr.reap_stale(3600) is True, "a stale VM must be reaped"
    assert not mgr.state_path.exists(), "state must be gone after reaping"


def test_reap_stale_uses_config_ttl_when_not_passed(tmp_path, fake_key_factory):
    """reap_stale() with no arg uses config.vm_ttl_seconds."""
    now = {"t": 1000.0}
    cfg = SandboxConfig(provider_name="dry-run", token="", ssh_user="root", vm_ttl_seconds=100)
    mgr = make_manager(
        tmp_path, config=cfg, fake_key_factory=fake_key_factory, clock=lambda: now["t"]
    )
    mgr.ensure_vm()
    now["t"] += 200
    assert mgr.reap_stale(100) is True, "stale VM must be reaped using config TTL"
    assert not mgr.state_path.exists(), "state must be gone after reaping"


# ── touch (last_used update on tool calls) ────────────────────────────────


def test_touch_updates_last_used(tmp_path, fake_key_factory):
    """touch() must advance last_used to the current clock time."""
    now = {"t": 1000.0}
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory, clock=lambda: now["t"])
    mgr.ensure_vm()
    original = mgr._repo.load()
    assert original is not None
    assert original.last_used == 1000.0

    now["t"] += 500
    assert mgr.touch() is True, "touch must return True when state exists"

    updated = mgr._repo.load()
    assert updated is not None
    assert updated.last_used == 1500.0, (
        f"last_used must be 1500.0 after touch, got {updated.last_used!r}"
    )


def test_touch_returns_false_when_no_state(tmp_path, fake_key_factory):
    """touch() must return False when there is no recorded state."""
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    assert mgr.touch() is False, "touch with no state must return False"


def test_touch_prevents_reaping(tmp_path, fake_key_factory):
    """A VM touched within the TTL window must survive reaping."""
    now = {"t": 1000.0}
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory, clock=lambda: now["t"])
    mgr.ensure_vm()

    # Advance past the TTL, then touch to reset last_used.
    now["t"] += 7200
    mgr.touch()

    # Advance only 60s — now within a 3600s TTL.
    now["t"] += 60
    assert mgr.reap_stale(3600) is False, "a recently touched VM must not be reaped"
    assert mgr.state_path.exists(), "state must survive when VM was recently touched"


# ── forward_env config parsing ─────────────────────────────────────────────


def test_config_forward_env_defaults_empty(monkeypatch):
    """Without SANDBOX_FORWARD_ENV, forward_env is an empty tuple."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.forward_env == (), "forward_env must default to empty tuple"


def test_config_forward_env_parsed_from_env(monkeypatch):
    """SANDBOX_FORWARD_ENV is parsed as a comma-separated list."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_FORWARD_ENV", "GITHUB_TOKEN,CODEBERG_TOKEN")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.forward_env == ("GITHUB_TOKEN", "CODEBERG_TOKEN"), (
        f"expected (GITHUB_TOKEN, CODEBERG_TOKEN), got {cfg.forward_env!r}"
    )


def test_config_forward_env_trims_whitespace(monkeypatch):
    """Whitespace around names is trimmed."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_FORWARD_ENV", " GITHUB_TOKEN , CODEBERG_TOKEN ")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.forward_env == ("GITHUB_TOKEN", "CODEBERG_TOKEN"), (
        f"whitespace must be trimmed, got {cfg.forward_env!r}"
    )


def test_config_forward_env_empty_string(monkeypatch):
    """An empty SANDBOX_FORWARD_ENV yields an empty tuple."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_FORWARD_ENV", "")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.forward_env == (), "empty string must yield empty tuple"


def test_config_forward_env_single_name(monkeypatch):
    """A single name without a comma works correctly."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_FORWARD_ENV", "GITHUB_TOKEN")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.forward_env == ("GITHUB_TOKEN",), (
        f"single name must yield a 1-tuple, got {cfg.forward_env!r}"
    )


# ── forward_env lifecycle threading ────────────────────────────────────────


def test_resolve_forward_env_returns_none_when_not_configured(tmp_path):
    """_resolve_forward_env returns None when config.forward_env is empty."""
    mgr = make_manager(tmp_path)
    assert mgr._resolve_forward_env() is None, (
        "empty forward_env must return None (no injection)"
    )


def test_resolve_forward_env_reads_from_environ(tmp_path, monkeypatch):
    """_resolve_forward_env reads values from os.environ for configured names."""
    cfg = SandboxConfig(
        provider_name="dry-run",
        token="",
        ssh_user="root",
        forward_env=("GITHUB_TOKEN", "CODEBERG_TOKEN", "MISSING_VAR"),
    )
    mgr = make_manager(tmp_path, config=cfg)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
    monkeypatch.setenv("CODEBERG_TOKEN", "cb_test456")
    monkeypatch.delenv("MISSING_VAR", raising=False)
    resolved = mgr._resolve_forward_env()
    assert resolved == {"GITHUB_TOKEN": "ghp_test123", "CODEBERG_TOKEN": "cb_test456"}, (
        f"only present vars must be resolved, got {resolved!r}"
    )


def test_resolve_forward_env_returns_none_when_all_missing(tmp_path, monkeypatch):
    """_resolve_forward_env returns None when no configured vars are in the env."""
    cfg = SandboxConfig(
        provider_name="dry-run",
        token="",
        ssh_user="root",
        forward_env=("MISSING_VAR_1", "MISSING_VAR_2"),
    )
    mgr = make_manager(tmp_path, config=cfg)
    monkeypatch.delenv("MISSING_VAR_1", raising=False)
    monkeypatch.delenv("MISSING_VAR_2", raising=False)
    assert mgr._resolve_forward_env() is None, (
        "all-missing forward_env must return None (no injection)"
    )


def test_ensure_vm_passes_forward_env_to_provider(tmp_path, fake_key_factory, monkeypatch):
    """ensure_vm() builds forward_env from the environment and passes it to provision()."""
    cfg = SandboxConfig(
        provider_name="dry-run",
        token="",
        ssh_user="root",
        forward_env=("GITHUB_TOKEN", "CODEBERG_TOKEN"),
    )
    provider = DryRunProvider()
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_lifecycle")
    monkeypatch.setenv("CODEBERG_TOKEN", "cb_lifecycle")
    mgr = make_manager(tmp_path, provider=provider, config=cfg, fake_key_factory=fake_key_factory)
    vm = mgr.ensure_vm()
    assert vm.extra.get("forward_env") == {
        "GITHUB_TOKEN": "ghp_lifecycle",
        "CODEBERG_TOKEN": "cb_lifecycle",
    }, f"forward_env must be passed through to the provider, got {vm.extra.get('forward_env')!r}"


def test_ensure_vm_no_forward_env_when_not_configured(tmp_path, fake_key_factory):
    """ensure_vm() does not set forward_env when config.forward_env is empty."""
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, fake_key_factory=fake_key_factory)
    vm = mgr.ensure_vm()
    assert vm.extra.get("forward_env") == {}, (
        f"forward_env must be empty dict when not configured, got {vm.extra.get('forward_env')!r}"
    )


# ── apt_repos / extra_packages / setup_commands config parsing ─────────────


def test_config_apt_repos_defaults_empty(monkeypatch):
    """Without SANDBOX_APT_REPOS, apt_repos is an empty tuple."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.apt_repos == (), "apt_repos must default to empty tuple"


def test_config_apt_repos_parsed_from_env(monkeypatch):
    """SANDBOX_APT_REPOS is parsed as newline-separated deb lines."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv(
        "SANDBOX_APT_REPOS",
        "deb https://packages.example.com stable main\ndeb https://apt.example.org focal main",
    )
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.apt_repos == (
        "deb https://packages.example.com stable main",
        "deb https://apt.example.org focal main",
    ), f"expected two repo lines, got {cfg.apt_repos!r}"


def test_config_apt_repos_single_line(monkeypatch):
    """A single deb line without a newline works correctly."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_APT_REPOS", "deb https://packages.example.com stable main")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.apt_repos == ("deb https://packages.example.com stable main",), (
        f"single repo line must yield a 1-tuple, got {cfg.apt_repos!r}"
    )


def test_config_apt_repos_empty_string(monkeypatch):
    """An empty SANDBOX_APT_REPOS yields an empty tuple."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_APT_REPOS", "")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.apt_repos == (), "empty string must yield empty tuple"


def test_config_extra_packages_defaults_empty(monkeypatch):
    """Without SANDBOX_PACKAGES, extra_packages is an empty tuple."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.extra_packages == (), "extra_packages must default to empty tuple"


def test_config_extra_packages_parsed_from_env(monkeypatch):
    """SANDBOX_PACKAGES is parsed as a comma-separated list."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_PACKAGES", "htop,vim,tmux")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.extra_packages == ("htop", "vim", "tmux"), (
        f"expected (htop, vim, tmux), got {cfg.extra_packages!r}"
    )


def test_config_extra_packages_trims_whitespace(monkeypatch):
    """Whitespace around package names is trimmed."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_PACKAGES", " htop , vim , tmux ")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.extra_packages == ("htop", "vim", "tmux"), (
        f"whitespace must be trimmed, got {cfg.extra_packages!r}"
    )


def test_config_extra_packages_single_name(monkeypatch):
    """A single package name without a comma works correctly."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_PACKAGES", "htop")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.extra_packages == ("htop",), (
        f"single package must yield a 1-tuple, got {cfg.extra_packages!r}"
    )


def test_config_extra_packages_empty_string(monkeypatch):
    """An empty SANDBOX_PACKAGES yields an empty tuple."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_PACKAGES", "")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.extra_packages == (), "empty string must yield empty tuple"


def test_config_setup_commands_defaults_empty(monkeypatch):
    """Without SANDBOX_SETUP_CMDS, setup_commands is an empty tuple."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.setup_commands == (), "setup_commands must default to empty tuple"


def test_config_setup_commands_parsed_from_env(monkeypatch):
    """SANDBOX_SETUP_CMDS is parsed as newline-separated commands."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv(
        "SANDBOX_SETUP_CMDS",
        "echo hello\ncurl -fsSL https://example.com/install.sh | sh",
    )
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.setup_commands == (
        "echo hello",
        "curl -fsSL https://example.com/install.sh | sh",
    ), f"expected two commands, got {cfg.setup_commands!r}"


def test_config_setup_commands_single_command(monkeypatch):
    """A single command without a newline works correctly."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_SETUP_CMDS", "echo hello")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.setup_commands == ("echo hello",), (
        f"single command must yield a 1-tuple, got {cfg.setup_commands!r}"
    )


def test_config_setup_commands_empty_string(monkeypatch):
    """An empty SANDBOX_SETUP_CMDS yields an empty tuple."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "dry-run")
    monkeypatch.setenv("SANDBOX_SETUP_CMDS", "")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.setup_commands == (), "empty string must yield empty tuple"


# ── apt_repos / extra_packages / setup_commands lifecycle threading ────────


def test_ensure_vm_passes_apt_repos_to_provider(tmp_path, fake_key_factory):
    """ensure_vm() passes apt_repos from config through to provision()."""
    cfg = SandboxConfig(
        provider_name="dry-run",
        token="",
        ssh_user="root",
        apt_repos=("deb https://repo.example.com stable main",),
    )
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, config=cfg, fake_key_factory=fake_key_factory)
    vm = mgr.ensure_vm()
    assert vm.extra.get("apt_repos") == ("deb https://repo.example.com stable main",), (
        f"apt_repos must be passed through to the provider, got {vm.extra.get('apt_repos')!r}"
    )


def test_ensure_vm_passes_extra_packages_to_provider(tmp_path, fake_key_factory):
    """ensure_vm() passes extra_packages from config through to provision()."""
    cfg = SandboxConfig(
        provider_name="dry-run",
        token="",
        ssh_user="root",
        extra_packages=("htop", "vim"),
    )
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, config=cfg, fake_key_factory=fake_key_factory)
    vm = mgr.ensure_vm()
    assert vm.extra.get("extra_packages") == ("htop", "vim"), (
        f"extra_packages must be passed through to the provider, got {vm.extra.get('extra_packages')!r}"
    )


def test_ensure_vm_passes_setup_commands_to_provider(tmp_path, fake_key_factory):
    """ensure_vm() passes setup_commands from config through to provision()."""
    cfg = SandboxConfig(
        provider_name="dry-run",
        token="",
        ssh_user="root",
        setup_commands=("echo hello",),
    )
    provider = DryRunProvider()
    mgr = make_manager(tmp_path, provider=provider, config=cfg, fake_key_factory=fake_key_factory)
    vm = mgr.ensure_vm()
    assert vm.extra.get("setup_commands") == ("echo hello",), (
        f"setup_commands must be passed through to the provider, got {vm.extra.get('setup_commands')!r}"
    )


# ── SSH readiness probing ──────────────────────────────────────────────────


def test_ensure_vm_probes_ssh_after_provisioning(tmp_path, fake_key_factory):
    """ensure_vm() must call the SSH probe after provisioning a new VM."""
    probe_calls: list[dict] = []

    def recording_probe(**kwargs):
        probe_calls.append(kwargs)
        return True

    mgr = make_manager(
        tmp_path,
        fake_key_factory=fake_key_factory,
        ssh_probe=recording_probe,
    )
    mgr.ensure_vm()
    assert len(probe_calls) == 1, (
        f"SSH probe must fire exactly once after provisioning, got {len(probe_calls)}"
    )
    assert probe_calls[0]["timeout"] == 120, (
        f"provision-timeout must be 120s, got {probe_calls[0]['timeout']}"
    )


def test_ensure_vm_probes_ssh_on_reuse(tmp_path, fake_key_factory):
    """ensure_vm() must probe SSH before reusing an existing VM."""
    probe_calls: list[dict] = []

    def recording_probe(**kwargs):
        probe_calls.append(kwargs)
        return True

    provider = DryRunProvider()
    mgr = make_manager(
        tmp_path,
        provider=provider,
        fake_key_factory=fake_key_factory,
        ssh_probe=recording_probe,
    )
    mgr.ensure_vm()
    probe_calls.clear()
    mgr.ensure_vm()
    assert len(probe_calls) == 1, (
        f"SSH probe must fire once on reuse, got {len(probe_calls)}"
    )
    assert probe_calls[0]["timeout"] == 30, (
        f"reuse-timeout must be 30s, got {probe_calls[0]['timeout']}"
    )


def test_ensure_vm_reprovisions_when_ssh_unreachable_on_reuse(tmp_path, fake_key_factory):
    """If SSH is unreachable on a recorded VM, ensure_vm must reprovision."""
    provider = DryRunProvider()
    mgr = make_manager(
        tmp_path,
        provider=provider,
        fake_key_factory=fake_key_factory,
        ssh_probe=lambda **kwargs: False,
    )
    first = mgr.ensure_vm()
    second = mgr.ensure_vm()
    assert second.id != first.id, (
        f"SSH-unreachable VM must be replaced, both were {first.id!r}"
    )
    provisions = [c for c in provider.calls if c["op"] == "provision"]
    assert len(provisions) == 2, (
        f"reprovisioning must provision twice, got {len(provisions)}"
    )


def test_ensure_vm_proceeds_when_ssh_not_ready_after_provisioning(tmp_path, fake_key_factory):
    """If SSH never becomes ready after provisioning, ensure_vm must still
    record state and return the VM (fail-open — the agent will retry)."""
    mgr = make_manager(
        tmp_path,
        fake_key_factory=fake_key_factory,
        ssh_probe=lambda **kwargs: False,
    )
    vm = mgr.ensure_vm()
    assert vm.is_running(), "VM must be returned even when SSH probe fails"
    assert mgr.state_path.exists(), "state must be recorded even when SSH probe fails"


def test_eager_wire_ssh_env_skips_when_ssh_unreachable(tmp_path, fake_key_factory, monkeypatch):
    """_eager_wire_ssh_env must set TERMINAL_ENV=ssh and TERMINAL_SSH_HOST
    from recorded state even when SSH is unreachable.

    The agent constructor probes the terminal backend before on_session_start
    fires, so the SSH env vars must be present regardless of VM liveness.
    on_session_start will reprovision if the VM is dead.
    """
    import os

    provider = DryRunProvider()
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(exist_ok=True)
    mgr = VMLifecycleManager(
        provider=provider,
        config=SandboxConfig(provider_name="dry-run", token="", ssh_user="root"),
        hermes_home=hermes_home,
        key_factory=fake_key_factory,
        ssh_probe=lambda **kwargs: False,
    )
    mgr.ensure_vm()
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.delenv("TERMINAL_SSH_HOST", raising=False)

    import hermes_sandbox
    from hermes_sandbox import _eager_wire_ssh_env

    orig_manager = hermes_sandbox._manager
    hermes_sandbox._manager = mgr
    try:
        _eager_wire_ssh_env()
    finally:
        hermes_sandbox._manager = orig_manager

    assert os.environ.get("TERMINAL_ENV") == "ssh", (
        "TERMINAL_ENV must be ssh even when SSH probe fails"
    )
    assert os.environ.get("TERMINAL_SSH_HOST"), (
        "TERMINAL_SSH_HOST must be set from recorded state even when SSH "
        "probe fails — the agent probe needs it before on_session_start fires"
    )
