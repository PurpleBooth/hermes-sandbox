"""Tests for the QEMU local hypervisor provider.

Unit tests use a fake runner to verify command construction without needing
QEMU installed.  The contract tests in ``test_provider_contract.py`` use the
real subprocess runner and skip when QEMU or an ISO builder (xorriso /
genisoimage / mkisofs) is not on PATH.
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from hermes_sandbox.provider import available_providers, get_provider
from hermes_sandbox.qemu import (
    QemuProvider,
    _build_cloud_init_seed,
    _default_downloader,
    _default_runner,
    _find_iso_builder,
    _iso_build_prefix,
    _kill_pidfile,
    _parse_instance_type,
)


def _fake_runner(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a fake subprocess runner that records every command."""
    calls: list[list[str]] = []

    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_qemu_is_registered():
    """QEMU is in the provider registry after import."""
    import hermes_sandbox.qemu  # noqa: F401

    assert "qemu" in available_providers()


def test_qemu_provider_name_and_defaults():
    p = QemuProvider()
    assert p.name == "qemu"
    assert p.default_region == "local"
    assert p.default_instance_type == "2cpu-4gb"


def test_qemu_accepts_empty_token():
    """QEMU needs no token — get_provider(name, token=...) must not crash."""
    p = get_provider("qemu", token="")
    assert isinstance(p, QemuProvider)


def test_qemu_provision_starts_vm_and_returns_running_instance():
    runner = _fake_runner()
    p = QemuProvider(runner=runner, iso_bin="genisoimage")
    vm = p.provision(
        name="test-vm",
        instance_type="2cpu-4gb",
        region="local",
        image="/data/ubuntu.qcow2",
        ssh_public_key="ssh-ed25519 FAKEKEY",
        labels={"hermes-sandbox": "true"},
    )
    assert vm.id
    assert vm.name == "test-vm"
    assert vm.is_running()
    assert vm.public_ip == "127.0.0.1"
    assert vm.region == "local"
    assert vm.provider == "qemu"
    # SSH port is a random ephemeral port allocated by the OS, stored in extra
    # so the lifecycle manager can wire TERMINAL_SSH_PORT.
    lo, hi = (int(x) for x in Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split())
    assert lo <= vm.extra["ssh_port"] <= hi, (
        f"expected kernel ephemeral port in [{lo}, {hi}], got {vm.extra['ssh_port']}"
    )
    # The runner is called three times: qemu-img (overlay), genisoimage (seed
    # ISO), and qemu-system-x86_64 (the VM).
    assert len(runner.calls) == 3, f"expected 3 runner calls, got {len(runner.calls)}"
    qemu_cmd = [c for c in runner.calls if "qemu-system-x86_64" in c[0]]
    assert len(qemu_cmd) == 1
    assert "-daemonize" in qemu_cmd[0]


def test_qemu_provision_parses_instance_type():
    """Instance type '4cpu-8gb' maps to -smp 4 -m 8192."""
    runner = _fake_runner()
    p = QemuProvider(runner=runner, iso_bin="genisoimage")
    p.provision(
        name="v",
        instance_type="4cpu-8gb",
        region="local",
        image="/img.qcow2",
        ssh_public_key="k",
    )
    qemu_cmd = [c for c in runner.calls if "qemu-system-x86_64" in c[0]][0]
    assert "-smp" in qemu_cmd
    assert qemu_cmd[qemu_cmd.index("-smp") + 1] == "4"
    assert "-m" in qemu_cmd
    assert qemu_cmd[qemu_cmd.index("-m") + 1] == "8192"


def test_qemu_provision_allocates_distinct_ssh_ports(monkeypatch):
    """Each VM gets a different SSH port, recorded in extra."""
    ports = iter([54321, 54322])
    monkeypatch.setattr(
        "hermes_sandbox.qemu.QemuProvider._next_ssh_port",
        lambda self: next(ports),
    )
    runner = _fake_runner()
    p = QemuProvider(runner=runner, iso_bin="genisoimage")
    vm1 = p.provision("v1", "2cpu-2gb", "local", "/img.qcow2", "k")
    vm2 = p.provision("v2", "2cpu-2gb", "local", "/img.qcow2", "k")
    assert vm1.extra["ssh_port"] == 54321, f"expected 54321, got {vm1.extra['ssh_port']}"
    assert vm2.extra["ssh_port"] == 54322, f"expected 54322, got {vm2.extra['ssh_port']}"


def test_qemu_get_returns_provisioned_vm():
    p = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage")
    vm = p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "k")
    fetched = p.get(vm.id)
    assert fetched.id == vm.id
    assert fetched.name == vm.name
    assert fetched.provider == "qemu"


def test_qemu_get_missing_raises_keyerror():
    p = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage")
    with pytest.raises(KeyError):
        p.get("nonexistent")


def test_qemu_teardown_sends_sigterm_and_removes_from_memory(monkeypatch, tmp_path):
    """teardown SIGTERMs the recorded QEMU PID (os.kill) and forgets the VM."""
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("hermes_sandbox.qemu.os.kill", lambda pid, sig: killed.append((pid, sig)))
    p = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage", work_dir=tmp_path)
    vm = p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "k")
    # provision() with a fake runner doesn't start QEMU, so write the pidfile
    # teardown() will read.
    pidfile = tmp_path / "v" / "qemu.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("4242")
    p.teardown(vm.id)
    assert (4242, signal.SIGTERM) in killed, "teardown must SIGTERM the recorded PID"
    with pytest.raises(KeyError):
        p.get(vm.id)


def test_qemu_teardown_missing_is_noop():
    """Tearing down a VM that doesn't exist must not raise."""
    p = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage")
    p.teardown("nonexistent")


def test_qemu_list_by_label_finds_provisioned_vm(tmp_path):
    p = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage", work_dir=tmp_path)
    p.provision(
        "v",
        "2cpu-2gb",
        "local",
        "/img.qcow2",
        "k",
        labels={"hermes-sandbox": "true"},
    )
    found = p.list_by_label("hermes-sandbox", "true")
    assert len(found) == 1, f"expected exactly one matching VM, got {len(found)}"
    assert found[0].provider == "qemu", f"expected provider qemu, got {found[0].provider!r}"


def test_qemu_list_by_label_empty_when_no_match(tmp_path):
    p = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage", work_dir=tmp_path)
    assert p.list_by_label("hermes-sandbox", "true") == [], (
        "list_by_label must be empty when no VM matches the label"
    )


def test_qemu_provision_includes_image_in_command(tmp_path):
    runner = _fake_runner()
    p = QemuProvider(runner=runner, iso_bin="genisoimage", work_dir=tmp_path)
    p.provision("v", "2cpu-2gb", "local", "/data/ubuntu.qcow2", "k")
    qemu_img_cmd = [c for c in runner.calls if c[0] == "qemu-img"]
    assert len(qemu_img_cmd) == 1, "provision must run qemu-img to create the overlay"
    assert "/data/ubuntu.qcow2" in qemu_img_cmd[0], (
        "base image must be the qcow2 backing file (-b) in the qemu-img command"
    )


def test_qemu_provision_builds_cloud_init_seed_iso():
    """provision() calls genisoimage/mkisofs to build a real cloud-init ISO."""
    runner = _fake_runner()
    p = QemuProvider(runner=runner, iso_bin="genisoimage")
    p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "ssh-ed25519 AAAAB3Nza...")
    iso_cmds = [c for c in runner.calls if c and c[0] in ("genisoimage", "mkisofs")]
    assert len(iso_cmds) == 1, "provision must call genisoimage/mkisofs to build the seed ISO"
    cmd = iso_cmds[0]
    assert "-volid" in cmd
    assert cmd[cmd.index("-volid") + 1] == "cidata"
    assert "-output" in cmd
    assert "cidata.iso" in cmd[cmd.index("-output") + 1]


def test_qemu_provision_writes_cloud_init_user_data(tmp_path):
    """The user-data creates a 'hermes' user, installs the toolchain, and embeds
    the SSH key.

    The exact tool list is an implementation detail; these assertions pin the
    high-value categories a user is likely to need: a build toolchain, common
    languages, media/TTS tools, and modern shell utilities.
    """
    runner = _fake_runner()
    p = QemuProvider(runner=runner, iso_bin="genisoimage", work_dir=tmp_path)
    p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "ssh-ed25519 AAAAB3Nza...")
    user_data = (p._work_dir / "v" / "cidata" / "user-data").read_text()
    assert "#cloud-config" in user_data, "user-data must be a cloud-config document"
    assert "name: hermes" in user_data, f"user-data must create a 'hermes' user, got:\n{user_data}"
    assert "ssh-ed25519 AAAAB3Nza..." in user_data, (
        "the provisioned SSH public key must be in the hermes user's authorized keys"
    )
    assert "sudo" in user_data, (
        "the hermes user must have sudo access (the sandbox runs root commands)"
    )
    # Tool categories that should be pre-installed via cloud-init.
    assert "packages:" in user_data, "user-data must install a toolchain via packages:"
    assert "ffmpeg" in user_data, "ffmpeg must be pre-installed (media/TTS pipelines)"
    assert "python3-venv" in user_data, "python3-venv must be pre-installed (execute_code)"
    assert "ripgrep" in user_data, "ripgrep must be pre-installed (search)"
    assert "jq" in user_data, "jq must be pre-installed (JSON wrangling)"
    assert "runcmd:" in user_data, (
        "user-data must have a runcmd: block for post-install setup (aliases, pip tools)"
    )
    assert "edge-tts" in user_data, "edge-tts must be installed via runcmd (open-source TTS)"


def test_qemu_default_ssh_user_is_hermes():
    """QEMU provider defaults to connecting as the 'hermes' user it creates."""
    p = QemuProvider()
    assert p.default_ssh_user == "hermes", (
        f"expected default_ssh_user='hermes' so the lifecycle connects as the "
        f"cloud-init user, got {p.default_ssh_user!r}"
    )


def test_qemu_provision_includes_seed_iso_in_qemu_command():
    """The QEMU command must include the cloud-init seed ISO as a drive."""
    runner = _fake_runner()
    p = QemuProvider(runner=runner, iso_bin="genisoimage")
    p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "k")
    qemu_cmd = [c for c in runner.calls if "qemu-system-x86_64" in c[0]][0]
    drive_args = [a for a in qemu_cmd if "cidata.iso" in a]
    assert drive_args, "QEMU command must include the cloud-init seed ISO"


# ── Image resolution / download ──────────────────────────────────────────────


def _fake_downloader(downloaded: dict[str, Path]):
    """Build a fake downloader that writes a dummy file and records the URL."""

    def downloader(url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake qcow2")
        downloaded["url"] = url
        downloaded["dest"] = dest

    return downloader


def test_qemu_default_image_is_ubuntu_url():
    """The default image is the Ubuntu 24.04 cloud image URL."""
    assert QemuProvider.default_image.startswith("https://")
    assert "ubuntu" in QemuProvider.default_image


def test_qemu_provision_downloads_image_when_url(tmp_path):
    """When the image is a URL, it is downloaded to the cache and the local
    path is used in the QEMU command."""
    runner = _fake_runner()
    downloaded: dict[str, Path] = {}
    downloader = _fake_downloader(downloaded)
    p = QemuProvider(runner=runner, iso_bin="genisoimage", work_dir=tmp_path, downloader=downloader)
    p.provision(
        "v",
        "2cpu-2gb",
        "local",
        "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img",
        "k",
    )
    assert downloaded["url"].startswith("https://cloud-images.ubuntu.com")
    qemu_cmd = [c for c in runner.calls if "qemu-system-x86_64" in c[0]][0]
    drive_args = [a for a in qemu_cmd if "qcow2" in a and "file=" in a]
    assert not any("https://" in a for a in drive_args), (
        "QEMU command must use a local path, not a URL"
    )
    assert downloaded["dest"].exists()


def test_qemu_provision_reuses_cached_image(tmp_path):
    """A second provision with the same URL reuses the cached download."""
    runner = _fake_runner()
    download_count = [0]

    def counting_downloader(url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake qcow2")
        download_count[0] += 1

    p = QemuProvider(
        runner=runner, iso_bin="genisoimage", work_dir=tmp_path, downloader=counting_downloader
    )
    url = "https://example.com/ubuntu.qcow2"
    p.provision("v1", "2cpu-2gb", "local", url, "k")
    p.provision("v2", "2cpu-2gb", "local", url, "k")
    assert download_count[0] == 1, "second provision should reuse the cached image"


def test_qemu_provision_uses_local_image_directly(tmp_path):
    """When the image is a local path that exists, no download happens."""
    runner = _fake_runner()
    download_count = [0]

    def counting_downloader(url: str, dest: Path) -> None:
        download_count[0] += 1

    local_img = tmp_path / "local.qcow2"
    local_img.write_bytes(b"existing image")
    p = QemuProvider(
        runner=runner, iso_bin="genisoimage", work_dir=tmp_path, downloader=counting_downloader
    )
    p.provision("v", "2cpu-2gb", "local", str(local_img), "k")
    assert download_count[0] == 0, "local image should not trigger a download"


# ── Instance-type parsing ────────────────────────────────────────────────────


def test_parse_instance_type_falls_back_for_unparseable():
    """Strings that don't match ``<n>cpu-<m>gb`` fall back to (2, 4096)."""
    smp, mem_mb = _parse_instance_type("test-size")
    assert smp == 2, f"expected fallback smp=2, got {smp}"
    assert mem_mb == 4096, f"expected fallback mem=4096, got {mem_mb}"


def test_parse_instance_type_parses_cpu_and_memory():
    """'4cpu-8gb' maps to smp=4, mem=8192 MB."""
    smp, mem_mb = _parse_instance_type("4cpu-8gb")
    assert smp == 4, f"expected smp=4, got {smp}"
    assert mem_mb == 8192, f"expected mem=8192 MB, got {mem_mb}"


# ── ISO binary discovery ─────────────────────────────────────────────────────


def test_find_iso_builder_raises_when_none_found(monkeypatch, tmp_path):
    """When no ISO builder is on PATH, FileNotFoundError is raised."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="No ISO builder found"):
        _find_iso_builder()


def test_find_iso_builder_returns_first_available(monkeypatch, tmp_path):
    """When genisoimage is on PATH, its path is returned."""
    fake_bin = tmp_path / "genisoimage"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _find_iso_builder() == str(fake_bin)


def test_find_iso_builder_finds_xorrisofs(monkeypatch, tmp_path):
    """xorrisofs is discovered when genisoimage/mkisofs are absent."""
    fake_bin = tmp_path / "xorrisofs"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _find_iso_builder() == str(fake_bin)


def test_find_iso_builder_falls_back_to_xorriso(monkeypatch, tmp_path):
    """bare xorriso is discovered when no flag-compatible frontend exists."""
    fake_bin = tmp_path / "xorriso"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _find_iso_builder() == str(fake_bin)


def test_iso_build_prefix_adds_emulation_for_xorriso():
    """xorriso needs -as mkisofs emulation; other binaries need nothing."""
    assert _iso_build_prefix("xorriso") == ["-as", "mkisofs"], (
        "xorriso must get the mkisofs emulation prefix"
    )
    assert _iso_build_prefix("/usr/bin/xorriso") == ["-as", "mkisofs"], (
        "full path to xorriso must still be detected via basename"
    )
    assert _iso_build_prefix("genisoimage") == [], "genisoimage needs no prefix"
    assert _iso_build_prefix("xorrisofs") == [], "xorrisofs needs no prefix"


# ── Cloud-init seed build errors ─────────────────────────────────────────────


def test_build_cloud_init_seed_raises_on_iso_build_failure(tmp_path):
    """When genisoimage exits non-zero, a RuntimeError is raised."""
    runner = _fake_runner(returncode=1, stderr="mkisofs: error")
    with pytest.raises(RuntimeError, match="Failed to build cloud-init seed ISO"):
        _build_cloud_init_seed("ssh-ed25519 k", tmp_path, runner, iso_bin="genisoimage")


def test_build_cloud_init_seed_removes_stale_iso(tmp_path):
    """A pre-existing cidata.iso is removed before rebuilding.

    xorriso rejects overwriting an output ISO whose size differs with a
    non-zero "size has changed" MISHAP, so reprovisioning the same VM name
    must start from a clean ISO path.
    """
    runner = _fake_runner()
    stale = tmp_path / "cidata" / "cidata.iso"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale iso")
    _build_cloud_init_seed("ssh-ed25519 k", tmp_path, runner, iso_bin="genisoimage")
    assert not stale.exists(), "stale ISO must be removed before rebuilding"


# ── Provision / teardown error paths ─────────────────────────────────────────


def _selective_runner(*, iso_ok: bool = True, qemu_ok: bool = True):
    """Runner that succeeds/fails per command type, recording every call."""
    calls: list[list[str]] = []

    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        head = cmd[0] if cmd else ""
        if head in ("genisoimage", "mkisofs", "xorrisofs", "xorriso"):
            rc = 0 if iso_ok else 1
        elif "qemu-system" in head:
            rc = 0 if qemu_ok else 1
        else:
            rc = 0
        return subprocess.CompletedProcess(cmd, rc, "", "" if rc == 0 else "error")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_provision_raises_when_qemu_fails_to_start():
    """If the QEMU process exits non-zero, provision() raises RuntimeError."""
    runner = _selective_runner(iso_ok=True, qemu_ok=False)
    p = QemuProvider(runner=runner, iso_bin="genisoimage")
    with pytest.raises(RuntimeError, match="QEMU failed to start VM"):
        p.provision("dead-vm", "2cpu-2gb", "local", "/img.qcow2", "k")


def test_teardown_handles_already_dead_process(monkeypatch, tmp_path):
    """If the QEMU PID is already gone, teardown is a no-op (no raise).

    os.kill on a dead PID raises ProcessLookupError, which teardown swallows.
    """

    def raise_gone(*_args: object) -> None:
        raise ProcessLookupError()

    monkeypatch.setattr("hermes_sandbox.qemu.os.kill", raise_gone)
    p = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage", work_dir=tmp_path)
    vm = p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "k")
    pidfile = tmp_path / "v" / "qemu.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("4242")
    p.teardown(vm.id)  # must not raise even though the PID is gone
    assert not pidfile.exists(), "teardown must still clean up the pidfile"
    with pytest.raises(KeyError):
        p.get(vm.id)


def test_kill_pidfile_is_noop_for_missing_or_malformed(tmp_path):
    """_kill_pidfile must never raise on a missing or bad pidfile."""
    missing = tmp_path / "nope.pid"
    _kill_pidfile(missing)  # no file — no raise, no error
    malformed = tmp_path / "bad.pid"
    malformed.write_text("not-a-number")
    _kill_pidfile(malformed)  # non-int contents — no raise
    assert malformed.exists(), "malformed pidfile is left untouched (not our process)"


def test_kill_pidfile_signals_pid_and_removes_file(monkeypatch, tmp_path):
    """_kill_pidfile SIGTERMs the recorded PID and removes the pidfile."""
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("hermes_sandbox.qemu.os.kill", lambda pid, sig: killed.append((pid, sig)))
    pidfile = tmp_path / "qemu.pid"
    pidfile.write_text("7777")
    _kill_pidfile(pidfile)
    assert killed == [(7777, signal.SIGTERM)], "must SIGTERM the exact PID"
    assert not pidfile.exists(), "pidfile must be removed after signalling"


# ── Default subprocess helpers ───────────────────────────────────────────────


def test_default_runner_runs_command_and_captures_result():
    """_default_runner shells out and returns the CompletedProcess."""
    ok = _default_runner(["/bin/true"])
    assert ok.returncode == 0, "expected /bin/true to exit 0"
    fail = _default_runner(["/bin/false"])
    assert fail.returncode != 0, "expected /bin/false to exit non-zero"


def test_default_downloader_downloads_file_url(tmp_path):
    """_default_downloader copies a URL to the destination path."""
    src = tmp_path / "source.img"
    src.write_bytes(b"image-bytes")
    dest = tmp_path / "out" / "downloaded.img"
    _default_downloader(src.as_uri(), dest)
    assert dest.read_bytes() == b"image-bytes", "downloaded content must match source"


# ── QCOW2 overlay (Bug 1: base-image corruption) ─────────────────────────────


def test_qemu_provision_creates_qcow2_overlay_backed_by_base_image(tmp_path):
    """provision() must create a per-VM qcow2 overlay backed by the base image
    and use the OVERLAY as the drive file — never the shared cached base image
    (which would be corrupted when two VMs share the same default URL).
    """
    runner = _fake_runner()
    local_base = tmp_path / "base.qcow2"
    local_base.write_bytes(b"base image")
    p = QemuProvider(runner=runner, iso_bin="genisoimage", work_dir=tmp_path)
    p.provision("v", "2cpu-2gb", "local", str(local_base), "k")

    img_cmds = [c for c in runner.calls if c[:2] == ["qemu-img", "create"]]
    assert len(img_cmds) == 1, f"expected one qemu-img create call, got {len(img_cmds)}"
    img_cmd = img_cmds[0]
    assert "-b" in img_cmd, "overlay must declare a backing file (-b)"
    backing = img_cmd[img_cmd.index("-b") + 1]
    assert backing == str(local_base), f"backing file must be the base image, got {backing!r}"
    assert "-F" in img_cmd, "overlay must declare backing format (-F qcow2)"
    fmt = img_cmd[img_cmd.index("-F") + 1]
    assert fmt == "qcow2", f"backing format must be qcow2, got {fmt!r}"
    overlay_path = img_cmd[-1]
    assert overlay_path == str(tmp_path / "v" / "disk.qcow2"), (
        f"overlay must be at work_dir/<name>/disk.qcow2, got {overlay_path!r}"
    )

    qemu_cmd = [c for c in runner.calls if "qemu-system-x86_64" in c[0]][0]
    drive_args = [a for a in qemu_cmd if "file=" in a]
    assert any(f"file={overlay_path}" in a for a in drive_args), (
        f"QEMU drive must use the overlay {overlay_path!r}"
    )
    assert not any(str(local_base) in a for a in drive_args), (
        "QEMU drive must NOT reference the shared base image directly"
    )


# ── Cross-process get()/list_by_label() (Bug 2) ──────────────────────────────


def test_qemu_get_works_across_separate_processes(tmp_path):
    """get() must read vm.json from disk so a SEPARATE process (a new provider
    instance sharing the same work_dir) can look up a VM it never provisioned
    in-memory.  Without this, the lifecycle manager re-provisions and orphans
    the running QEMU process.
    """
    p1 = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage", work_dir=tmp_path)
    vm = p1.provision("v", "2cpu-2gb", "local", "/img.qcow2", "k", labels={"env": "test"})

    p2 = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage", work_dir=tmp_path)
    fetched = p2.get(vm.id)
    assert fetched.id == vm.id, f"expected id={vm.id!r}, got {fetched.id!r}"
    assert fetched.name == vm.name, f"expected name={vm.name!r}, got {fetched.name!r}"
    assert fetched.provider == "qemu", f"expected provider='qemu', got {fetched.provider!r}"


def test_qemu_list_by_label_works_across_separate_processes(tmp_path):
    """list_by_label() must scan vm.json files on disk so a separate process
    can discover VMs created by another process.
    """
    p1 = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage", work_dir=tmp_path)
    p1.provision("v", "2cpu-2gb", "local", "/img.qcow2", "k", labels={"env": "prod"})

    p2 = QemuProvider(runner=_fake_runner(), iso_bin="genisoimage", work_dir=tmp_path)
    found = p2.list_by_label("env", "prod")
    assert len(found) == 1, f"expected 1 VM with env=prod, got {len(found)}"
    assert found[0].id == "v", f"expected VM id 'v', got {found[0].id!r}"


# ── KVM acceleration (Bug 3) ─────────────────────────────────────────────────


def test_qemu_provision_enables_kvm_by_default():
    """By default provision() launches QEMU with -enable-kvm for hardware
    acceleration (TCG software emulation is 10–50× slower).
    """
    runner = _fake_runner()
    p = QemuProvider(runner=runner, iso_bin="genisoimage")
    p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "k")
    qemu_cmd = [c for c in runner.calls if "qemu-system-x86_64" in c[0]][0]
    assert "-enable-kvm" in qemu_cmd, "default provision must enable KVM acceleration"


def test_qemu_provision_can_disable_kvm():
    """When enable_kvm=False, the -enable-kvm flag is omitted (TCG fallback)."""
    runner = _fake_runner()
    p = QemuProvider(runner=runner, iso_bin="genisoimage", enable_kvm=False)
    p.provision("v", "2cpu-2gb", "local", "/img.qcow2", "k")
    qemu_cmd = [c for c in runner.calls if "qemu-system-x86_64" in c[0]][0]
    assert "-enable-kvm" not in qemu_cmd, "enable_kvm=False must not pass -enable-kvm to QEMU"
