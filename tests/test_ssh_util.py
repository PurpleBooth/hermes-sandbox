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
    assert kp.public_key.startswith("ssh-ed25519 ")
    assert kp.public_key.endswith("hermes-sandbox")  # hardcoded comment
    assert kp.private_path.endswith("test-key")
    assert kp.public_path.endswith("test-key.pub")
    # Private key is owner-only (ssh-keygen creates 0600).
    assert stat.S_IMODE((tmp_path / "test-key").stat().st_mode) == 0o600
    assert (tmp_path / "test-key.pub").exists()


@needs_ssh_keygen
def test_generate_keypair_reuses_existing_key(tmp_path):
    first = generate_keypair(tmp_path, "reuse-key")
    # Second call must NOT clobber — a running VM may still trust the public half.
    second = generate_keypair(tmp_path, "reuse-key")
    assert first.public_key == second.public_key


@needs_ssh_keygen
def test_generate_keypair_is_idempotent_across_calls(tmp_path):
    a = generate_keypair(tmp_path, "idem")
    b = generate_keypair(tmp_path, "idem")
    assert a == b


def test_wait_for_ssh_returns_false_on_closed_port(tmp_path):
    """Against a port nothing listens on, wait_for_ssh must fail fast and
    return False — not hang.  Uses a tiny timeout so the test stays quick."""
    from hermes_sandbox.ssh_util import wait_for_ssh

    # Generate a real key so the probe gets far enough to fail on connection.
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen not on PATH")
    kp = generate_keypair(tmp_path, "probe-key")

    # 127.0.0.1:9 (discard) is almost always closed.  Short timeout + interval.
    result = wait_for_ssh(
        host="127.0.0.1", user="root", key_path=kp.private_path,
        port=9, timeout=2, interval=0.1,
    )
    assert result is False


def test_chmod_private_key_and_restrict_to_owner_set_0600(tmp_path):
    f = tmp_path / "secret"
    f.write_text("x")
    f.chmod(0o644)
    chmod_private_key(str(f))
    assert stat.S_IMODE(f.stat().st_mode) == 0o600

    f.chmod(0o644)
    restrict_to_owner(str(f))
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_restrict_to_owner_is_noop_on_missing_path(tmp_path):
    # Must not raise when the path doesn't exist.
    restrict_to_owner(str(tmp_path / "absent"))
