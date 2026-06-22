"""Tests for ssh_util — exercise the real ssh-keygen subprocess and the
wait_for_ssh failure path (no live SSH server required).

These cover the security-relevant keypair code that the unit tests stub out
via the ``fake_key_factory`` fixture.  ssh-keygen must be on PATH (it is on
any normal dev box / CI image).
"""

from __future__ import annotations

import shutil
import stat

import pytest

from hermes_sandbox.ssh_util import chmod_private_key, generate_keypair, restrict_to_owner

needs_ssh_keygen = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="ssh-keygen not on PATH"
)


@needs_ssh_keygen
def test_generate_keypair_creates_real_ed25519_key(tmp_path):
    kp = generate_keypair(tmp_path, "test-key")
    assert kp.public_key.startswith("ssh-ed25519 "), (
        f"public key must be ed25519, got {kp.public_key!r}"
    )
    assert kp.public_key.endswith("hermes-sandbox"), (
        f"public key comment must be 'hermes-sandbox', got {kp.public_key!r}"
    )
    assert kp.private_path.endswith("test-key"), (
        f"private path must end with test-key, got {kp.private_path!r}"
    )
    assert kp.public_path.endswith("test-key.pub"), (
        f"public path must end with test-key.pub, got {kp.public_path!r}"
    )
    assert stat.S_IMODE((tmp_path / "test-key").stat().st_mode) == 0o600, (
        f"private key must be 0600, got {stat.S_IMODE((tmp_path / 'test-key').stat().st_mode):#o}"
    )
    assert (tmp_path / "test-key.pub").exists(), "public key file must exist"


@needs_ssh_keygen
def test_generate_keypair_reuses_existing_key(tmp_path):
    """A second call must not clobber an existing key (a running VM may trust it)."""
    first = generate_keypair(tmp_path, "reuse-key")
    second = generate_keypair(tmp_path, "reuse-key")
    assert first.public_key == second.public_key, (
        "reuse must return the same public key, not clobber it"
    )


@needs_ssh_keygen
def test_generate_keypair_is_idempotent_across_calls(tmp_path):
    a = generate_keypair(tmp_path, "idem")
    b = generate_keypair(tmp_path, "idem")
    assert a == b, "idempotent calls must return equal KeyPairs"


def test_wait_for_ssh_returns_false_on_closed_port(tmp_path):
    """Against a port nothing listens on, wait_for_ssh must fail fast and
    return False — not hang."""
    from hermes_sandbox.ssh_util import wait_for_ssh

    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen not on PATH")
    kp = generate_keypair(tmp_path, "probe-key")

    result = wait_for_ssh(
        host="127.0.0.1",
        user="root",
        key_path=kp.private_path,
        port=9,
        timeout=2,
        interval=0.1,
    )
    assert result is False, "wait_for_ssh against a closed port must return False, not hang"


def test_chmod_private_key_and_restrict_to_owner_set_0600(tmp_path):
    f = tmp_path / "secret"
    f.write_text("x")
    f.chmod(0o644)
    chmod_private_key(str(f))
    assert stat.S_IMODE(f.stat().st_mode) == 0o600, (
        f"chmod_private_key must set 0600, got {stat.S_IMODE(f.stat().st_mode):#o}"
    )

    f.chmod(0o644)
    restrict_to_owner(str(f))
    assert stat.S_IMODE(f.stat().st_mode) == 0o600, (
        f"restrict_to_owner must set 0600, got {stat.S_IMODE(f.stat().st_mode):#o}"
    )


def test_restrict_to_owner_is_noop_on_missing_path(tmp_path):
    """restrict_to_owner must not raise when the path doesn't exist."""
    restrict_to_owner(str(tmp_path / "absent"))
