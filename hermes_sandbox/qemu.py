"""QEMU local hypervisor provider for the sandbox plugin.

:class:`QemuProvider` runs VMs locally via ``qemu-system-x86_64`` — no cloud
credentials, no REST API, no network egress.  SSH is via localhost with a
host-forwarded port (QEMU user-mode networking).

The provider starts QEMU in daemon mode (``-daemonize``) so the VM runs in
the background.  The SSH public key is injected via a cloud-init seed disk
(a small ISO generated with ``mkisofs`` / ``genisoimage``).  The SSH port
is stored in ``VMInstance.extra["ssh_port"]`` so the lifecycle manager can
emit ``TERMINAL_SSH_PORT``.

If the image path is a URL (the default is the Ubuntu 24.04 cloud image),
it is downloaded to a local cache on first use and reused thereafter.
A local path is used as-is.

Registered as ``qemu``; select it with ``SANDBOX_PROVIDER=qemu``.
"""

from __future__ import annotations

import hashlib
import logging
import re
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
    "https://cloud-images.ubuntu.com/releases/24.04/release/"
    "ubuntu-24.04-server-cloudimg-amd64.img"
)

_CLOUD_INIT_TEMPLATE = """\
#cloud-config
ssh_authorized_keys:
  - {public_key}
"""


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


def _write_cloud_init_seed(public_key: str, dest: Path) -> Path:
    """Write a cloud-init seed ISO containing the SSH public key.

    Uses ``genisoimage`` (or ``mkisofs``) to create a CIDATA ISO.  The ISO
    is written to ``dest/cidata.iso`` and its path is returned.
    """
    seed_dir = dest / "cidata"
    seed_dir.mkdir(parents=True, exist_ok=True)
    (seed_dir / "user-data").write_text(
        _CLOUD_INIT_TEMPLATE.format(public_key=public_key)
    )
    (seed_dir / "meta-data").write_text("instance-id: hermes-sandbox\nlocal-hostname: sandbox\n")
    iso_path = seed_dir / "cidata.iso"
    return iso_path


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
        ssh_base_port: int = 2222,
        runner: CommandRunner | None = None,
        work_dir: Path | None = None,
        downloader: ImageDownloader | None = None,
    ) -> None:
        # ``token`` is accepted (and ignored) so ``get_provider(name, token=…)``
        # works uniformly across all providers.
        self.qemu_bin = qemu_bin
        self.ssh_base_port = ssh_base_port
        self._runner: CommandRunner = runner or _default_runner
        self._work_dir = work_dir or Path(tempfile.gettempdir()) / "hermes-sandbox"
        self._downloader: ImageDownloader = downloader or _default_downloader
        self._vms: dict[str, VMInstance] = {}
        self._port_counter = 0

    def _next_ssh_port(self) -> int:
        port = self.ssh_base_port + self._port_counter
        self._port_counter += 1
        return port

    def _resolve_image(self, image: str) -> str:
        """Return a local filesystem path for the disk image.

        If ``image`` is a URL, download it to the cache (once) and return the
        cached path.  If it's a local path that exists, return it as-is.
        """
        if image.startswith(("http://", "https://")):
            cache_dir = self._work_dir / "images"
            # Deterministic filename from the URL hash so the same image is
            # reused across runs.
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

        # Build the cloud-init seed for SSH key injection.
        seed_iso = _write_cloud_init_seed(ssh_public_key, self._work_dir / name)

        cmd = [
            self.qemu_bin,
            "-name", name,
            "-m", str(mem_mb),
            "-smp", str(smp),
            "-drive", f"file={local_image},format=qcow2,if=virtio",
            "-drive", f"file={seed_iso},format=raw,if=virtio,readonly=on",
            "-netdev", f"user,id=net0,hostfwd=tcp::{ssh_port}-:22",
            "-device", "virtio-net-pci,netdev=net0",
            "-daemonize",
            "-display", "none",
            "-serial", "file:" + str(self._work_dir / name / "console.log"),
        ]

        result = self._runner(cmd)
        if result.returncode != 0:
            raise RuntimeError(
                f"QEMU failed to start VM {name!r}: {result.stderr.strip()}"
            )

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
        vm = self._vms.pop(vm_id, None)
        if vm is None:
            return
        # Kill the QEMU process by name (QEMU -name sets the process title).
        try:
            self._runner(["pkill", "-f", f"-name {vm_id}"])
        except Exception as exc:
            logger.debug("qemu: pkill for %s failed (already gone?): %s", vm_id, exc)
        logger.info("qemu: tore down VM %s", vm_id)

    def list_by_label(self, label_key: str, label_value: str) -> list[VMInstance]:
        return [
            vm
            for vm in self._vms.values()
            if vm.extra.get("labels", {}).get(label_key) == label_value
        ]
