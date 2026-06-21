"""SSH keypair management and readiness probing for vm-sandbox."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class KeyPair:
    private_path: str
    public_path: str
    public_key: str  # full "ssh-ed25519 AAAA... comment" string


def generate_keypair(key_dir: Path, name: str = "hermes-vm-sandbox") -> KeyPair:
    """Generate an ed25519 keypair at ``<key_dir>/<name>`` (no passphrase).

    If the private key already exists it is reused — we never clobber an
    existing key, since a running VM may still trust its public half.
    """
    key_dir = Path(key_dir)
    key_dir.mkdir(parents=True, exist_ok=True)
    private = key_dir / name
    public = key_dir / f"{name}.pub"

    if not private.exists():
        ssh_keygen = shutil.which("ssh-keygen") or "ssh-keygen"
        cmd = [
            ssh_keygen,
            "-t", "ed25519",
            "-N", "",
            "-C", "hermes-vm-sandbox",
            "-f", str(private),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ssh-keygen failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        # ssh-keygen creates 0600 private / 0644 public — already correct.

    public_key = public.read_text(encoding="utf-8").strip()
    return KeyPair(
        private_path=str(private),
        public_path=str(public),
        public_key=public_key,
    )


def wait_for_ssh(
    host: str,
    user: str,
    key_path: str,
    port: int = 22,
    timeout: int = 180,
    interval: float = 3.0,
) -> bool:
    """Poll until an SSH connection succeeds or ``timeout`` elapses.

    Uses BatchMode + a short ConnectTimeout so a dead host fails fast
    rather than blocking the whole poll budget.  Returns True on success.

    Host key checking is accept-new (TOFU) — the VM was just provisioned
    by us, so its host key is trustworthy on first contact.
    """
    ssh = shutil.which("ssh") or "ssh"
    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        cmd = [
            ssh,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=5",
            "-o", "IdentitiesOnly=yes",
            "-i", key_path,
            "-p", str(port),
            f"{user}@{host}",
            "true",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                return True
            last_err = result.stderr.strip() or result.stdout.strip()
        except subprocess.TimeoutExpired:
            last_err = "ssh command timed out"
        time.sleep(interval)
    logger.warning("SSH readiness probe failed for %s@%s: %s", user, host, last_err)
    return False


def chmod_private_key(path: str) -> None:
    """Ensure a private key is 0600 — OpenSSH refuses group/other-readable keys."""
    if os.path.exists(path):
        os.chmod(path, 0o600)
