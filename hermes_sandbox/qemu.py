"""QEMU local hypervisor provider for the sandbox plugin.

:class:`QemuProvider` runs VMs locally via ``qemu-system-x86_64`` — no cloud
credentials, no REST API, no network egress.  SSH is via localhost with a
host-forwarded port (QEMU user-mode networking).

The provider starts QEMU in daemon mode (``-daemonize``) so the VM runs in
the background.  The SSH public key is injected via a cloud-init NoCloud
seed disk: a small CIDATA ISO built with ``xorriso`` (or ``genisoimage`` /
``mkisofs``) containing ``user-data`` and ``meta-data``.  The SSH port is stored in
``VMInstance.extra["ssh_port"]`` so the lifecycle manager can emit
``TERMINAL_SSH_PORT``.

If the image path is a URL (the default is the Ubuntu 24.04 cloud image),
it is downloaded to a local cache on first use and reused thereafter.
A local path is used as-is.

Registered as ``qemu``; select it with ``SANDBOX_PROVIDER=qemu``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .models import VMInstance
from .provider import CloudProvider, register_provider

logger = logging.getLogger(__name__)

#: Type of the injectable subprocess runner (mirrors ``subprocess.run``).
CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]

#: Type of the injectable image downloader: (url, dest_path) -> None.
ImageDownloader = Callable[[str, Path], None]

#: Default Ubuntu 24.04 server cloud image (qcow2, ~600 MB).
DEFAULT_UBUNTU_IMAGE_URL = (
    "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img"
)

_CLOUD_INIT_USER_DATA = """\
#cloud-config
ssh_authorized_keys:
  - {public_key}
"""

_CLOUD_INIT_META_DATA = "instance-id: hermes-sandbox\nlocal-hostname: sandbox\n"


def _parse_instance_type(instance_type: str) -> tuple[int, int]:
    """Parse ``<ncpu>-<mem>gb`` into ``(smp, mem_mb)``.

    Falls back to ``(2, 4096)`` when the string doesn't match, so the provider
    works with arbitrary instance-type strings from contract tests.

    >>> _parse_instance_type("2cpu-4gb")
    (2, 4096)
    """
    match = re.match(r"(\d+)cpu-(\d+)gb", instance_type)
    if not match:
        return 2, 4096
    return int(match.group(1)), int(match.group(2)) * 1024


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command via subprocess.run with a 60s timeout."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _default_downloader(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` via stdlib urllib."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("qemu: downloading image from %s", url)
    urllib.request.urlretrieve(url, dest)
    logger.info("qemu: image cached at %s", dest)


def _find_iso_builder() -> str:
    """Return the first available ISO-building binary, or raise FileNotFoundError.

    ``genisoimage``, ``mkisofs`` and ``xorrisofs`` all accept the same
    command-line flags, so any of them is used directly.  ``xorriso`` itself
    is supported via its ``-as mkisofs`` emulation mode (see
    :func:`_iso_build_prefix`).
    """
    for binary in ("genisoimage", "mkisofs", "xorrisofs"):
        path = shutil.which(binary)
        if path:
            return path
    path = shutil.which("xorriso")
    if path:
        return path
    raise FileNotFoundError(
        "No ISO builder found — install xorriso (or genisoimage/cdrkit) to "
        "use the QEMU sandbox provider."
    )


def _iso_build_prefix(binary: str) -> list[str]:
    """Extra args prepended before the standard mkisofs-style flags.

    ``xorriso`` needs ``-as mkisofs`` to enter its mkisofs emulation mode;
    every other supported binary accepts the flags directly.
    """
    if Path(binary).name == "xorriso":
        return ["-as", "mkisofs"]
    return []


def _build_cloud_init_seed(
    public_key: str,
    dest: Path,
    runner: CommandRunner,
    iso_bin: str | None = None,
) -> Path:
    """Build a cloud-init NoCloud seed ISO containing the SSH public key.

    Writes ``user-data`` and ``meta-data`` to a ``cidata/`` directory, then
    calls an ISO builder (``genisoimage``, ``mkisofs``, ``xorrisofs`` or
    ``xorriso``) to create a CIDATA ISO at ``dest/cidata/cidata.iso``.
    The ISO path is returned.

    ``iso_bin`` overrides the auto-detected binary (for tests).  When None,
    :func:`_find_iso_builder` is called to find one on PATH.
    """
    seed_dir = dest / "cidata"
    seed_dir.mkdir(parents=True, exist_ok=True)
    (seed_dir / "user-data").write_text(_CLOUD_INIT_USER_DATA.format(public_key=public_key))
    (seed_dir / "meta-data").write_text(_CLOUD_INIT_META_DATA)

    binary = iso_bin or _find_iso_builder()
    iso_path = seed_dir / "cidata.iso"
    # A stale ISO at this path makes xorriso emit a "size has changed" MISHAP
    # and exit non-zero (genisoimage/mkisofs would just overwrite it); remove
    # it so every build — including reprovisioning the same VM name — is clean.
    iso_path.unlink(missing_ok=True)
    cmd = [
        binary,
        *_iso_build_prefix(binary),
        "-volid",
        "cidata",
        "-joliet",
        "-rock",
        "-output",
        str(iso_path),
        str(seed_dir),
    ]
    result = runner(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to build cloud-init seed ISO: {result.stderr.strip()}")
    logger.info("qemu: built cloud-init seed ISO at %s", iso_path)
    return iso_path


def _kill_pidfile(pidfile: Path) -> None:
    """SIGTERM the PID recorded in ``pidfile`` (if any) and remove the file.

    Uses :func:`os.kill` directly rather than exec'ing ``pkill`` — the PID is
    known exactly (QEMU wrote it via ``-pidfile``), so there is no need to
    match a process by name.  A missing or malformed pidfile is a no-op (the
    VM may already be gone).
    """
    try:
        pid = int(pidfile.read_text().strip())
    except (FileNotFoundError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # already exited
    except OSError as exc:
        logger.debug("qemu: os.kill(%d) failed: %s", pid, exc)
    pidfile.unlink(missing_ok=True)


@register_provider
class QemuProvider(CloudProvider):
    """Local QEMU hypervisor provider."""

    name = "qemu"
    token_env_keys = ("SANDBOX_TOKEN",)
    default_region = "local"
    default_image = DEFAULT_UBUNTU_IMAGE_URL
    default_instance_type = "2cpu-4gb"

    def __init__(
        self,
        token: str = "",
        qemu_bin: str = "qemu-system-x86_64",
        runner: CommandRunner | None = None,
        work_dir: Path | None = None,
        downloader: ImageDownloader | None = None,
        iso_bin: str | None = None,
    ) -> None:
        # ``token`` is accepted (and ignored) so ``get_provider(name, …)``
        # works uniformly across all providers.
        self.qemu_bin = qemu_bin
        self._runner: CommandRunner = runner or _default_runner
        self._work_dir = work_dir or Path(tempfile.gettempdir()) / "hermes-sandbox"
        self._downloader: ImageDownloader = downloader or _default_downloader
        self._iso_bin = iso_bin  # resolved lazily via _find_iso_builder() if None
        self._vms: dict[str, VMInstance] = {}

    def _next_ssh_port(self) -> int:
        """Allocate a free ephemeral port from the OS.

        Binds a socket to port 0 and lets the kernel pick a free port from the
        ephemeral range (32768–60999 on Linux).  The socket is closed before returning
        so QEMU can bind it immediately for the host-forward.

        Unlike a fixed/incrementing base port this is collision-free across
        processes: every ``provision()`` gets a port the kernel has just
        confirmed is free, regardless of how many VMs a previous process left
        running.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("", 0))
            return int(sock.getsockname()[1])
        finally:
            sock.close()

    def _resolve_image(self, image: str) -> str:
        """Return a local filesystem path for the disk image.

        If ``image`` is a URL, download it to the cache (once) and return the
        cached path.  If it's a local path, return it as-is.
        """
        if image.startswith(("http://", "https://")):
            cache_dir = self._work_dir / "images"
            url_hash = hashlib.sha256(image.encode()).hexdigest()[:16]
            filename = image.rsplit("/", 1)[-1]
            cached = cache_dir / f"{url_hash}-{filename}"
            if not cached.exists():
                self._downloader(image, cached)
            return str(cached)
        return image

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
        smp, mem_mb = _parse_instance_type(instance_type)
        ssh_port = self._next_ssh_port()

        # Resolve the image: download if it's a URL, use local path as-is.
        local_image = self._resolve_image(image)

        # Build the cloud-init NoCloud seed ISO for SSH key injection.
        seed_iso = _build_cloud_init_seed(
            ssh_public_key, self._work_dir / name, self._runner, self._iso_bin
        )

        # QEMU records its daemon PID here so teardown can SIGTERM it directly.
        pidfile = self._work_dir / name / "qemu.pid"
        cmd = [
            self.qemu_bin,
            "-name",
            name,
            "-m",
            str(mem_mb),
            "-smp",
            str(smp),
            "-drive",
            f"file={local_image},format=qcow2,if=virtio",
            "-drive",
            f"file={seed_iso},format=raw,if=virtio,readonly=on",
            "-netdev",
            f"user,id=net0,hostfwd=tcp::{ssh_port}-:22",
            "-device",
            "virtio-net-pci,netdev=net0",
            "-daemonize",
            "-display",
            "none",
            "-pidfile",
            str(pidfile),
            "-serial",
            "file:" + str(self._work_dir / name / "console.log"),
        ]

        result = self._runner(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"QEMU failed to start VM {name!r}: {result.stderr.strip()}")

        vm = VMInstance(
            id=name,
            name=name,
            status="running",
            public_ip="127.0.0.1",
            region="local",
            provider=self.name,
            extra={
                "ssh_port": ssh_port,
                "labels": labels or {},
                "instance_type": instance_type,
                "image": image,
            },
        )
        self._vms[vm.id] = vm
        logger.info("qemu: started VM %s (ssh port %d)", vm.id, ssh_port)
        return vm

    def get(self, vm_id: str) -> VMInstance:
        if vm_id not in self._vms:
            raise KeyError(vm_id)
        return self._vms[vm_id]

    def teardown(self, vm_id: str) -> None:
        # Drop the in-memory entry if present (same-process usage).  ``hermes
        # sandbox teardown`` is a separate process, so the registry is usually
        # empty here — the VM is found via its deterministic pidfile path
        # (``work_dir/<name>/qemu.pid``), which survives across processes.
        self._vms.pop(vm_id, None)
        _kill_pidfile(self._work_dir / vm_id / "qemu.pid")
        logger.info("qemu: tore down VM %s", vm_id)

    def list_by_label(self, label_key: str, label_value: str) -> list[VMInstance]:
        return [
            vm
            for vm in self._vms.values()
            if vm.extra.get("labels", {}).get(label_key) == label_value
        ]
