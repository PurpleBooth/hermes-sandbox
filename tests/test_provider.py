"""Tests for the provider abstraction and registry."""

import dataclasses

import pytest
import yaml

from hermes_sandbox.models import VMInstance
from hermes_sandbox.provider import (
    CloudProvider,
    available_providers,
    get_provider,
    register_provider,
)


def test_vminstance_is_frozen():
    """VMInstance is an immutable value object — fields can't be reassigned."""
    vm = VMInstance("1", "n", "running", "1.2.3.4", "r", "p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        vm.status = "off"  # type: ignore[misc]


def test_vminstance_is_running():
    assert VMInstance("1", "n", "running", "1.2.3.4", "r", "p").is_running(), (
        "'running' status must be considered running"
    )
    assert not VMInstance("1", "n", "off", "1.2.3.4", "r", "p").is_running(), (
        "'off' status must not be considered running"
    )


def test_vminstance_preferred_ip_prefers_ipv4():
    vm = VMInstance("1", "n", "running", "1.2.3.4", "r", "p", public_ipv6="::1")
    assert vm.preferred_ip() == "1.2.3.4", (
        f"preferred_ip must return IPv4 when present, got {vm.preferred_ip()!r}"
    )
    vm6 = VMInstance("1", "n", "running", "", "r", "p", public_ipv6="::1")
    assert vm6.preferred_ip() == "::1", (
        f"preferred_ip must fall back to IPv6, got {vm6.preferred_ip()!r}"
    )
    vm4 = VMInstance("1", "n", "running", "1.2.3.4", "r", "p")
    assert vm4.preferred_ip() == "1.2.3.4", (
        f"preferred_ip must return IPv4 when no IPv6, got {vm4.preferred_ip()!r}"
    )


def test_vminstance_from_dict_drops_unknown_keys():
    """from_dict is the inverse of to_dict: unknown keys are ignored, not folded."""
    vm = VMInstance.from_dict(
        {
            "id": "1",
            "name": "n",
            "status": "running",
            "public_ip": "1.2.3.4",
            "region": "r",
            "provider": "p",
            "bogus": "dropped",
        }
    )
    assert vm.id == "1", f"expected id '1', got {vm.id!r}"
    assert vm.extra == {}, f"unknown keys must not be folded into extra, got {vm.extra!r}"


def test_vminstance_from_dict_preserves_extra():
    vm = VMInstance.from_dict(
        {
            "id": "1",
            "name": "n",
            "status": "running",
            "public_ip": "1.2.3.4",
            "region": "r",
            "provider": "p",
            "extra": {"foo": "bar"},
        }
    )
    assert vm.extra == {"foo": "bar"}, f"extra must be preserved, got {vm.extra!r}"


def test_vminstance_roundtrip():
    vm = VMInstance(
        "1", "n", "running", "1.2.3.4", "r", "p", public_ipv6="::1", extra={"foo": "bar"}
    )
    d = vm.to_dict()
    assert d["public_ipv6"] == "::1", f"to_dict must expose public_ipv6, got {d['public_ipv6']!r}"
    restored = VMInstance.from_dict(d)
    assert restored.public_ipv6 == "::1", (
        f"roundtrip must preserve public_ipv6, got {restored.public_ipv6!r}"
    )
    assert restored.extra["foo"] == "bar", f"roundtrip must preserve extra, got {restored.extra!r}"


def test_registry_has_dry_run_hetzner_digitalocean():
    names = available_providers()
    assert "dry-run" in names, f"dry-run must be registered, got {names!r}"
    import hermes_sandbox.digitalocean  # noqa: F401
    import hermes_sandbox.hetzner  # noqa: F401

    assert "hetzner" in available_providers(), "hetzner must register on import"
    assert "digitalocean" in available_providers(), "digitalocean must register on import"


def test_get_provider_unknown_raises():
    with pytest.raises(KeyError):
        get_provider("nonexistent")


def test_register_provider_requires_name():
    with pytest.raises(ValueError):

        @register_provider
        class Nameless(CloudProvider):
            name = ""

            def provision(self, *a, **k): ...
            def get(self, vm_id): ...
            def teardown(self, vm_id): ...
            def list_by_label(self, k, v):
                return []


def test_dry_run_provider_provision_get_teardown():
    from hermes_sandbox.dry_run import DryRunProvider

    p = DryRunProvider()
    vm = p.provision("v", "t", "r", "img", "ssh-key", labels={"x": "y"})
    assert vm.is_running(), f"provisioned VM must be running, status={vm.status!r}"
    assert vm.public_ip, "provisioned VM must have a public IP"
    assert len(vm.id) == 36, f"expected a 36-char UUID, got {vm.id!r}"
    assert vm.id.count("-") == 4, f"expected 4 hyphens in the UUID, got {vm.id!r}"
    assert p.get(vm.id).id == vm.id, "get() must return the same VM by id"
    assert p.list_by_label("x", "y") == [vm], "list_by_label must find the provisioned VM"
    p.teardown(vm.id)
    with pytest.raises(KeyError):
        p.get(vm.id)


def test_poll_until_returns_when_predicate_true():
    from hermes_sandbox.provider import poll_until

    calls = {"n": 0}

    def pred() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    slept: list[float] = []
    poll_until(pred, interval=0, timeout=5, sleep=slept.append)
    assert calls["n"] == 3, f"predicate must be called exactly 3 times, got {calls['n']}"
    assert len(slept) == 2, f"must sleep between tries (2 sleeps), got {len(slept)}"


def test_poll_until_raises_on_timeout():
    from hermes_sandbox.provider import poll_until

    slept: list[float] = []
    ticks = iter([0.0, 100.0, 100.0])

    def fake_clock() -> float:
        return next(ticks)

    with pytest.raises(TimeoutError):
        poll_until(lambda: False, interval=0, timeout=1, sleep=slept.append, clock=fake_clock)
    assert slept == [], "must never sleep when the deadline already passed after the first check"


# ── helpers ─────────────────────────────────────────────────────────────────


def _parse_user_data(data: str) -> dict:
    """Parse cloud-init user-data (strip the ``#cloud-config`` header) into a dict."""
    return yaml.safe_load(data[len("#cloud-config") :])


# ── SSH hardening ────────────────────────────────────────────────────────────


def test_build_cloud_init_disables_password_authentication():
    """Password auth must be disabled at both the cloud-init and sshd levels."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    data = build_cloud_init_user_data("ssh-ed25519 AAAA")
    assert "ssh_pwauth: false" in data, "ssh_pwauth must be false (disable password auth)"

    config = _parse_user_data(data)
    assert config["ssh_pwauth"] is False, "ssh_pwauth must be False in the parsed config"

    sshd_entries = [f for f in config["write_files"] if "sshd_config" in f["path"]]
    assert len(sshd_entries) == 1, "there must be exactly one sshd drop-in config"
    assert "PasswordAuthentication no" in sshd_entries[0]["content"], (
        "sshd config must set PasswordAuthentication no"
    )


def test_build_cloud_init_blocks_root_login():
    """Root login over SSH must be explicitly blocked."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    sshd_config = [f for f in config["write_files"] if "sshd_config" in f["path"]][0]
    assert "PermitRootLogin no" in sshd_config["content"], "sshd config must set PermitRootLogin no"


def test_build_cloud_init_requires_pubkey_authentication():
    """Public key authentication must be explicitly enabled."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    sshd_config = [f for f in config["write_files"] if "sshd_config" in f["path"]][0]
    assert "PubkeyAuthentication yes" in sshd_config["content"], (
        "sshd config must set PubkeyAuthentication yes"
    )


def test_build_cloud_init_restarts_sshd_before_other_runcmd():
    """The sshd restart must be the first runcmd entry so hardening is active early."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    assert config["runcmd"][0] == "systemctl restart ssh", (
        "the first runcmd entry must be 'systemctl restart ssh' (apply SSH hardening early)"
    )


def test_build_cloud_init_user_data_is_valid_yaml():
    """The generated user-data must parse as valid YAML after the header line."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    data = build_cloud_init_user_data(
        "ssh-ed25519 AAAA",
        forward_env={"FOO": "bar"},
        apt_repos=("deb https://example.com stable main",),
        extra_packages=("custom-pkg",),
        setup_commands=("echo done",),
    )
    parsed = _parse_user_data(data)
    assert isinstance(parsed, dict), "parsed cloud-config must be a dict"
    assert "users" in parsed, "cloud-config must have a users section"


def test_build_cloud_init_creates_hermes_user_with_key():
    """The user-data must create a 'hermes' user with the injected SSH key."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAAKEY"))
    assert len(config["users"]) == 1, "there must be exactly one user"
    user = config["users"][0]
    assert user["name"] == "hermes", f"user must be 'hermes', got {user['name']!r}"
    assert "ssh-ed25519 AAAAKEY" in user["ssh_authorized_keys"], (
        "the SSH key must be in the hermes user's authorized_keys"
    )


def test_build_cloud_init_hermes_user_has_sudo():
    """The hermes user must have passwordless sudo so root operations work."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    user = config["users"][0]
    assert "NOPASSWD" in user["sudo"], (
        "the hermes user must have passwordless sudo for root operations"
    )


# ── AGENTS.md seeding ─────────────────────────────────────────────────────


def test_build_cloud_init_seeds_agents_md_in_home():
    """AGENTS.md must be written to /home/hermes/AGENTS.md via write_files."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    agents_files = [f for f in config["write_files"] if f["path"] == "/home/hermes/AGENTS.md"]
    assert len(agents_files) == 1, (
        "there must be exactly one write_files entry for /home/hermes/AGENTS.md"
    )
    entry = agents_files[0]
    assert entry["content"].startswith("# Hermes Sandbox"), (
        "AGENTS.md content must start with the '# Hermes Sandbox' title"
    )
    assert entry["owner"] == "hermes:hermes", (
        f"AGENTS.md owner must be hermes:hermes, got {entry.get('owner')!r}"
    )
    assert entry["permissions"] == "0644", (
        f"AGENTS.md permissions must be 0644, got {entry.get('permissions')!r}"
    )


# ── forward_env → cloud-init write_files ───────────────────────────────────


def test_build_cloud_init_without_forward_env_has_no_etc_environment_append(tmp_path):
    """Without forward_env the user-data must NOT append to /etc/environment."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    runcmd_str = " ".join(config["runcmd"])
    assert "/etc/environment" not in runcmd_str, (
        "no /etc/environment append when forward_env is empty"
    )


def test_build_cloud_init_with_forward_env_injects_etc_environment():
    """forward_env values are appended to /etc/environment via runcmd."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    data = build_cloud_init_user_data(
        "ssh-ed25519 AAAA",
        forward_env={"GITHUB_TOKEN": "ghp_secret", "CODEBERG_TOKEN": "cb_secret"},
    )
    config = _parse_user_data(data)
    runcmd_str = " ".join(config["runcmd"])
    assert "'GITHUB_TOKEN'" in runcmd_str and "'ghp_secret'" in runcmd_str, (
        "the GITHUB_TOKEN assignment must be in runcmd"
    )
    assert "'CODEBERG_TOKEN'" in runcmd_str and "'cb_secret'" in runcmd_str, (
        "the CODEBERG_TOKEN assignment must be in runcmd"
    )
    assert ">> /etc/environment" in runcmd_str, (
        "runcmd must append to /etc/environment (not clobber it)"
    )
    env_files = [f for f in config["write_files"] if f["path"] == "/etc/environment"]
    assert env_files == [], "no write_files entry for /etc/environment (uses runcmd instead)"


def test_build_cloud_init_forward_env_empty_dict_no_etc_environment():
    """An empty forward_env dict must NOT inject an /etc/environment entry."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA", forward_env={}))
    runcmd_str = " ".join(config["runcmd"])
    assert "/etc/environment" not in runcmd_str, (
        "empty forward_env dict must not touch /etc/environment"
    )


def test_build_cloud_init_forward_env_none_no_etc_environment():
    """A None forward_env must NOT inject an /etc/environment entry."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA", forward_env=None))
    runcmd_str = " ".join(config["runcmd"])
    assert "/etc/environment" not in runcmd_str, "None forward_env must not touch /etc/environment"


def test_build_cloud_init_forward_env_prepends_to_runcmd():
    """forward_env prepends /etc/environment append to the existing runcmd list."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(
        build_cloud_init_user_data(
            "ssh-ed25519 AAAA",
            forward_env={"FOO": "bar"},
        )
    )
    paths = [f["path"] for f in config["write_files"]]
    assert "/etc/ssh/sshd_config.d/99-sandbox-hardening.conf" in paths, (
        "SSH hardening write_files entry must still be present"
    )
    assert "/etc/environment" not in paths, (
        "/etc/environment must NOT be in write_files (uses runcmd instead)"
    )
    first_runcmd = config["runcmd"][0]
    assert "/etc/environment" in first_runcmd, "first runcmd entry must append to /etc/environment"
    assert "'FOO'" in first_runcmd and "'bar'" in first_runcmd, (
        "the forwarded var must be in the runcmd"
    )


# ── netrc_content → cloud-init write_files ─────────────────────────────────


def test_build_cloud_init_without_netrc_has_no_netrc_file():
    """Without netrc_content the user-data must NOT write /home/hermes/.netrc."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    paths = [f["path"] for f in config["write_files"]]
    assert "/home/hermes/.netrc" not in paths, "no .netrc write_files entry by default"


def test_build_cloud_init_with_netrc_writes_file():
    """netrc_content is written to /home/hermes/.netrc with mode 0600."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    body = "machine github.com\nlogin x-access-token\npassword ghp_test\n"
    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA", netrc_content=body))
    entries = [f for f in config["write_files"] if f["path"] == "/home/hermes/.netrc"]
    assert len(entries) == 1, "exactly one .netrc write_files entry"
    entry = entries[0]
    assert entry["content"] == body
    assert entry["permissions"] == "0600"
    assert entry["owner"] == "hermes:hermes"


def test_build_cloud_init_netrc_empty_string_no_file():
    """An empty netrc_content string must not write .netrc."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA", netrc_content=""))
    paths = [f["path"] for f in config["write_files"]]
    assert "/home/hermes/.netrc" not in paths


def test_build_cloud_init_netrc_none_no_file():
    """netrc_content=None must not write .netrc."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA", netrc_content=None))
    paths = [f["path"] for f in config["write_files"]]
    assert "/home/hermes/.netrc" not in paths


def test_build_cloud_init_netrc_keeps_other_write_files():
    """Adding .netrc must not drop SSH hardening or AGENTS.md entries."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(
        build_cloud_init_user_data(
            "ssh-ed25519 AAAA",
            netrc_content="machine x\nlogin y\npassword z\n",
        )
    )
    paths = {f["path"] for f in config["write_files"]}
    assert "/etc/ssh/sshd_config.d/99-sandbox-hardening.conf" in paths
    assert "/home/hermes/AGENTS.md" in paths
    assert "/home/hermes/.netrc" in paths


# ── apt_repos → cloud-init apt_sources ──────────────────────────────────────


def test_build_cloud_init_without_apt_repos_has_no_apt_sources():
    """Without apt_repos the user-data must NOT contain an apt_sources key."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    assert "apt_sources" not in config, "no apt_sources key when apt_repos is empty"


def test_build_cloud_init_with_apt_repos_injects_apt_sources():
    """apt_repos lines are injected as apt_sources entries before packages."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    repos = ("deb https://packages.example.com stable main",)
    data = build_cloud_init_user_data("ssh-ed25519 AAAA", apt_repos=repos)
    config = _parse_user_data(data)
    assert "apt_sources" in config, "apt_sources key must be present"
    assert config["apt_sources"] == [{"source": "deb https://packages.example.com stable main"}], (
        "apt_sources must contain the repo line"
    )

    # apt_sources must appear before packages so the repos are available.
    apt_idx = data.index("apt_sources:")
    pkg_idx = data.index("packages:")
    assert apt_idx < pkg_idx, "apt_sources must appear before packages in the cloud-config"


def test_build_cloud_init_with_multiple_apt_repos():
    """Multiple apt repos all appear in the apt_sources block."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    repos = (
        "deb https://packages.example.com stable main",
        "deb https://apt.example.org focal main",
    )
    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA", apt_repos=repos))
    assert len(config["apt_sources"]) == 2, "both repos must appear in apt_sources"
    assert config["apt_sources"][0]["source"] == "deb https://packages.example.com stable main"
    assert config["apt_sources"][1]["source"] == "deb https://apt.example.org focal main"


def test_build_cloud_init_empty_apt_repos_no_block():
    """An empty apt_repos tuple must NOT inject an apt_sources key."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA", apt_repos=()))
    assert "apt_sources" not in config, "empty apt_repos must not inject an apt_sources key"


# ── extra_packages → cloud-init packages list ───────────────────────────────


def test_build_cloud_init_without_extra_packages_unchanged():
    """Without extra_packages the user-data contains only the base packages."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    assert "my-custom-tool" not in config["packages"], (
        "no custom packages when extra_packages is empty"
    )


def test_build_cloud_init_with_extra_packages_appends_to_list():
    """extra_packages are appended to the packages list (before runcmd)."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(
        build_cloud_init_user_data(
            "ssh-ed25519 AAAA",
            extra_packages=("my-custom-tool", "another-pkg"),
        )
    )
    assert "my-custom-tool" in config["packages"], "first extra package must appear in packages"
    assert "another-pkg" in config["packages"], "second extra package must appear in packages"


def test_build_cloud_init_empty_extra_packages_unchanged():
    """An empty extra_packages tuple must not add any new package entries."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA", extra_packages=()))
    base = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    assert config["packages"] == base["packages"], (
        "empty extra_packages must leave packages list unchanged"
    )


# ── setup_commands → cloud-init runcmd list ─────────────────────────────────


def test_build_cloud_init_without_setup_commands_unchanged():
    """Without setup_commands the user-data has no custom runcmd entries."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    assert "echo custom-setup-done" not in config["runcmd"], (
        "no custom commands when setup_commands is empty"
    )


def test_build_cloud_init_with_setup_commands_appends_to_runcmd():
    """setup_commands are appended to the runcmd list."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    cmds = ("echo custom-setup-done", "curl -fsSL https://example.com/install.sh | sh")
    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA", setup_commands=cmds))
    assert "echo custom-setup-done" in config["runcmd"], "first setup command must appear in runcmd"
    assert "curl -fsSL https://example.com/install.sh | sh" in config["runcmd"], (
        "second setup command must appear in runcmd"
    )
    # Setup commands must appear after the existing runcmd entries.
    base = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    base_count = len(base["runcmd"])
    assert config["runcmd"][base_count:] == list(cmds), (
        "custom commands must be appended after the existing runcmd entries"
    )


def test_build_cloud_init_empty_setup_commands_unchanged():
    """An empty setup_commands tuple must not add any new runcmd entries."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    config = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA", setup_commands=()))
    base = _parse_user_data(build_cloud_init_user_data("ssh-ed25519 AAAA"))
    assert config["runcmd"] == base["runcmd"], (
        "empty setup_commands must leave runcmd list unchanged"
    )


# ── combined injection ─────────────────────────────────────────────────────


def test_build_cloud_init_all_injections_together():
    """apt_repos, extra_packages, and setup_commands can all be set at once."""
    from hermes_sandbox.provider import build_cloud_init_user_data

    data = build_cloud_init_user_data(
        "ssh-ed25519 AAAA",
        apt_repos=("deb https://repo.example.com stable main",),
        extra_packages=("custom-pkg",),
        setup_commands=("echo done",),
    )
    config = _parse_user_data(data)
    assert "apt_sources" in config, "apt_sources must be present"
    assert "custom-pkg" in config["packages"], "extra package must be present"
    assert "echo done" in config["runcmd"], "setup command must be present"

    # Ordering in the serialized YAML: apt_sources → packages → runcmd.
    assert data.index("apt_sources:") < data.index("packages:"), "repos before packages"
    assert data.index("packages:") < data.index("runcmd:"), "packages before runcmd"
    assert data.index("runcmd:") < data.index("echo done"), "runcmd before custom commands"
