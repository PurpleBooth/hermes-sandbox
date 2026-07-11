"""Shared fixtures for sandbox plugin tests.

Adds the repo root to sys.path so ``import hermes_sandbox`` resolves, and
provides a fake keypair factory so tests don't shell out to ssh-keygen.
"""

import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _clean_terminal_env(monkeypatch):
    """Strip any leftover TERMINAL_* env so tests start from a clean slate.

    Also patches wait_for_ssh to a no-op so tests using DryRunProvider
    don't hang trying to SSH to a fake 10.x.x.x address.
    """
    for k in list(os.environ):
        if (
            k.startswith("TERMINAL_")
            or k.startswith("SANDBOX")
            or k
            in {
                "HCLOUD_TOKEN",
                "DIGITALOCEAN_TOKEN",
                "DO_API_TOKEN",
            }
        ):
            monkeypatch.delenv(k, raising=False)
    # Patch the default SSH probe so tests using DryRunProvider don't hang
    # trying to SSH to a fake 10.x.x.x address. Tests that need to assert on
    # probe behaviour pass ssh_probe= explicitly to VMLifecycleManager.
    import hermes_sandbox.lifecycle as _lifecycle

    monkeypatch.setattr(_lifecycle, "_DEFAULT_SSH_PROBE", lambda **kwargs: True)
    yield


@pytest.fixture
def fake_key_factory(tmp_path):
    """A keypair factory that writes real (empty) key files — no ssh-keygen."""
    from hermes_sandbox.ssh_util import KeyPair

    counter = {"n": 0}

    def _factory(key_dir: Path, name: str = "hermes-sandbox") -> KeyPair:
        key_dir = Path(key_dir)
        key_dir.mkdir(parents=True, exist_ok=True)
        counter["n"] += 1
        priv = key_dir / name
        pub = key_dir / f"{name}.pub"
        priv.write_text("PRIVATE-KEY-FAKE\n")
        pub.write_text("ssh-ed25519 FAKEKEY hermes-sandbox\n")
        os.chmod(priv, 0o600)
        return KeyPair(
            private_path=str(priv),
            public_path=str(pub),
            public_key=pub.read_text().strip(),
        )

    return _factory


@pytest.fixture
def fake_ssh_probe():
    """A no-op SSH probe that always reports the VM as reachable."""
    return lambda **kwargs: True
