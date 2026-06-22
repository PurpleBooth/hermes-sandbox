"""Property-based tests for the pure domain logic of sandbox.

These exercise invariants that hold for *any* input, not just hand-picked
examples — the kind of bug (off-by-one, wrong-default, broken roundtrip) that
example tests miss but randomised inputs catch.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from tempfile import mkdtemp

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hermes_sandbox.config import SandboxConfig
from hermes_sandbox.models import VMInstance
from hermes_sandbox.provider import poll_until
from hermes_sandbox.state import JsonStateRepository, SandboxState

# A non-empty text strategy for identifiers / values that must be meaningful.
# Env var names/values cannot contain NUL (os.environ rejects it), so we
# restrict the alphabet to printable-ish characters — a real domain constraint.
_safe_chars = st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00")
nonempty_text = st.text(alphabet=_safe_chars, min_size=1, max_size=40)
# Env-var-name-safe text (identifier-ish).
env_name = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_-"),
    min_size=1,
    max_size=30,
)
# IPv4-ish / IPv6-ish strings (we don't validate format, just exercise logic).
ip_text = st.text(alphabet=_safe_chars, min_size=1, max_size=40)

vminstance_strategy = st.builds(
    VMInstance,
    id=nonempty_text,
    name=nonempty_text,
    status=nonempty_text,
    public_ip=ip_text,
    region=nonempty_text,
    provider=nonempty_text,
    public_ipv6=st.text(max_size=40),
    extra=st.dictionaries(nonempty_text, st.text(max_size=40)),
)


# ── VMInstance: to_dict / from_dict roundtrip ──────────────────────────────


@given(vminstance_strategy)
def test_vminstance_to_dict_from_dict_is_identity(vm: VMInstance):
    """Round-tripping any VMInstance through dict yields an equal value."""
    restored = VMInstance.from_dict(vm.to_dict())
    assert restored == vm


@given(vminstance_strategy)
def test_vminstance_to_dict_has_all_fields(vm: VMInstance):
    """to_dict exposes every dataclass field."""
    for field in dataclasses.fields(vm):
        assert field.name in vm.to_dict()


# ── preferred_ip: IPv6 always wins when present ────────────────────────────


@given(ip_text, ip_text)
def test_preferred_ip_returns_ipv6_when_present(ipv4: str, ipv6: str):
    vm = VMInstance("1", "n", "running", ipv4, "r", "p", public_ipv6=ipv6)
    assert vm.preferred_ip() == ipv6


@given(ip_text)
def test_preferred_ip_falls_back_to_ipv4_when_no_ipv6(ipv4: str):
    vm = VMInstance("1", "n", "running", ipv4, "r", "p", public_ipv6="")
    assert vm.preferred_ip() == ipv4


# ── resolve_token: first non-empty key wins ────────────────────────────────


@given(
    pairs=st.lists(
        st.tuples(env_name, st.text(alphabet=_safe_chars, max_size=20)),
        min_size=1,
        max_size=5,
        unique_by=lambda kv: kv[0],
    ),
)
def test_resolve_token_picks_first_non_empty(pairs):
    from hermes_sandbox.provider import CloudProvider

    keys = [k for k, _ in pairs]
    env = dict(pairs)

    class _Probe(CloudProvider):
        name = "probe"
        token_env_keys = tuple(keys)

    expected = ""
    for k in keys:
        if env.get(k):
            expected = env[k]
            break
    assert _Probe.resolve_token(env) == expected


# ── SandboxConfig.from_env: explicit overrides win ────────────────────────


# monkeypatch is function-scoped; hypothesis warns about reusing it across
# examples.  That's fine here — we overwrite the same keys every time.  Cap
# examples: this test mutates os.environ, which is slower than pure functions.
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(
    region=nonempty_text,
    image=nonempty_text,
    instance_type=nonempty_text,
)
def test_config_from_env_overrides_win(monkeypatch, region, image, instance_type):
    """A SANDBOX_* env var always beats the provider's default."""
    monkeypatch.setenv("SANDBOX_PROVIDER", "hetzner")
    monkeypatch.setenv("HCLOUD_TOKEN", "tok")
    monkeypatch.setenv("SANDBOX_REGION", region)
    monkeypatch.setenv("SANDBOX_IMAGE", image)
    monkeypatch.setenv("SANDBOX_INSTANCE_TYPE", instance_type)
    cfg = SandboxConfig.from_env(dict(os.environ))
    assert cfg.region == region
    assert cfg.image == image
    assert cfg.instance_type == instance_type


# ── poll_until: invariant on predicate call count ─────────────────────────


@given(st.integers(min_value=1, max_value=20))
def test_poll_until_calls_predicate_at_least_once(n: int):
    calls = {"n": 0}

    def pred() -> bool:
        calls["n"] += 1
        return calls["n"] >= n

    sleeps: list[float] = []
    poll_until(pred, interval=0, timeout=1000, sleep=sleeps.append)
    assert calls["n"] == n
    # Sleeps happen between tries, never after success: n-1 sleeps.
    assert len(sleeps) == n - 1


def test_poll_until_never_sleeps_on_immediate_success():
    sleeps: list[float] = []
    poll_until(lambda: True, interval=5, timeout=1000, sleep=sleeps.append)
    assert sleeps == []


def test_poll_until_always_raises_on_timeout():
    # A clock that is already past the deadline after the first check.
    ticks = iter([0.0, 1000.0])
    with pytest.raises(TimeoutError):
        poll_until(
            lambda: False, interval=0, timeout=1, sleep=lambda _: None, clock=lambda: next(ticks)
        )


# ── SandboxState: typed persistence roundtrip ──────────────────────────────


_state_strategy = st.builds(
    SandboxState,
    vm_id=nonempty_text,
    provider=nonempty_text,
    name=nonempty_text,
    region=nonempty_text,
    public_ip=ip_text,
    public_ipv6=st.text(alphabet=_safe_chars, max_size=40),
    ssh_host=ip_text,
    ssh_user=nonempty_text,
    created_at=st.floats(min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False),
    last_used=st.floats(min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False),
)


@given(_state_strategy)
def test_json_state_repository_save_load_roundtrip(state):
    """save() then load() returns an equal SandboxState for any value."""
    repo = JsonStateRepository(Path(mkdtemp()))
    repo.save(state)
    assert repo.load() == state


@given(_state_strategy)
def test_sandbox_state_touched_advances_last_used_only(state):
    """touched(now) preserves every field except last_used."""
    touched = state.touched(123.0)
    assert touched.last_used == 123.0
    for field in dataclasses.fields(state):
        if field.name != "last_used":
            assert getattr(touched, field.name) == getattr(state, field.name)
