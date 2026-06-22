# sandbox DDD / Quality / Functional Refactor Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Refactor `hermes_sandbox/` to improve DDD layering, test quality, best
practices, and functional style — in small verified TDD steps, no big-bang
rewrite.

**Architecture:** Keep the flat `hermes_sandbox/` package (plugin discovery depends
on `register` in `__init__`). Make targeted improvements: extract value objects,
remove dead code, fix a latent DigitalOcean bug, DRY the poll loops, narrow
exception handling, and restore encapsulation between the plugin entrypoint and
the lifecycle manager.

**Tech Stack:** Python 3.11, stdlib only, pytest + ruff + mypy strict.

**Baseline (before refactor):** 68 tests pass, ruff clean, mypy clean, 75% cov.

---

## Review findings (the "why" for each task)

### DDD
- `lifecycle.py` fuses three layers in one module: `SandboxConfig` (value
  object), `VMLifecycleManager` (use-case), and state-file I/O (infra). Out of
  scope to split into subpackages now (risk to plugin discovery), but we can
  restore encapsulation: `__init__._apply_ssh_env` reaches into
  `manager._load_state()` (private) — Task 9.
- `status()` returns an untyped `dict[str, Any]` mutated in place; no domain
  object. The slash handler `json.dumps` it. A `SandboxStatus` value object
  types the boundary and the specdown `status` grep (`vm_id`/`provider`/
  `public_ip`) keeps passing as long as those field names survive — Task 3.
- `SandboxConfig.from_env` hardcodes a `if provider_name == "digitalocean"`
  branch for defaults — Open/Closed violation. Defaults belong on the provider
  class (like `token_env_keys`) — Task 4.

### Test quality
- **Latent bug, masked by a passing contract test:** `DigitalOceanProvider
  .list_by_label` hardcodes the tag `_TAG` and ignores `label_key`/`label_value`.
  `test_list_by_label_finds_provisioned_vm` passes only because it passes
  `label_key="hermes-sandbox"`, which matches the hardcoded tag — Task 1.
- `ssh_util.py` is 36% covered; `generate_keypair`/`wait_for_ssh` real paths
  untested. Out of scope to add network/subprocess tests, but Task 5 makes the
  provider poll loops injectable so they can be exercised without real sleep.
- `VMInstance.from_dict` has dead "fold unknown keys into extra" logic that does
  NOT do what its comment says — Task 7.
- Contract tests never assert on the request *body* sent by `provision`, so
  field mapping drift is invisible — Task 1 adds a body assertion for DO.

### Best practices
- Dead `logs` slash subcommand reads `sandbox.log`, a file nothing writes.
  Not in `docs/references/cli.md`. YAGNI — Task 2 removes it.
- `_load_state` catches bare `Exception` — Task 8 narrows it.
- `_save_state` calls `chmod_private_key(state_path)` on a JSON file; the helper
  is documented for private keys. Misleading — Task 10.
- Broad `except Exception` in fail-open *hooks* is intentional (documented in
  the skill) and left alone.

### Functional / monadic
- Both providers duplicate an imperative `while ... time.sleep ...` poll loop
  with no injectable sleep. Extract `poll_until(predicate, interval, timeout,
  sleep)` returning the polled value — Task 5.
- `VMInstance` is mutable; DO normalises status by mutating `vm.status =
  "running"`. Freeze the dataclass and use `dataclasses.replace` — Task 6.

---

## Task 1: Fix DigitalOcean `list_by_label` to honor `label_key`

**Objective:** `list_by_label(key, value)` queries `/droplets?tag_name={key}`
(the passed key) instead of the hardcoded tag. DO tags have no value, so
`label_value` is ignored (documented).

**Files:** `hermes_sandbox/digitalocean.py`, `tests/test_providers_api.py`

**Step 1 — failing test** in `tests/test_providers_api.py`:

```python
def test_do_list_by_label_uses_label_key_as_tag(monkeypatch):
    seen = []
    def fake_request(method, path, token, body=None, timeout=30):
        seen.append(path)
        if path == "/droplets?tag_name=custom-tag":
            return {"droplets": [DO_DROPLET]}
        return {"droplets": []}
    p = DigitalOceanProvider(token="tok", request_fn=fake_request)
    found = p.list_by_label("custom-tag", "ignored")
    assert seen == ["/droplets?tag_name=custom-tag"]
    assert len(found) == 1
```

**Step 2 — run, verify FAIL** (`just test-one tests/test_providers_api.py::test_do_list_by_label_uses_label_key_as_tag`).

**Step 3 — fix** in `digitalocean.py::list_by_label`:
```python
def list_by_label(self, label_key: str, label_value: str) -> list[VMInstance]:
    # DigitalOcean tags have no value component; the tag name is the label key.
    resp = self._request("GET", f"/droplets?tag_name={label_key}", self.token)
    return [self._to_vm_running_aware(d) for d in resp.get("droplets", [])]
```

**Step 4 — verify PASS**, then `just test` + `just lint`.

---

## Task 2: Remove dead `logs` slash subcommand

**Objective:** YAGNI — nothing writes `sandbox.log`; the subcommand is
undocumented in `docs/references/cli.md`.

**Files:** `hermes_sandbox/__init__.py`

**Step 1 — test** (adjust existing): `test_slash_help` already only asserts
`"/sandbox" in out`. Add an assertion that `logs` is no longer advertised:
```python
def test_slash_help_does_not_advertise_dead_logs():
    out = hermes_sandbox._handle_slash("help")
    assert "logs" not in out
```
Run → FAIL.

**Step 2 — remove** the `logs` branch from `_handle_slash` and the `logs` line
from `_HELP`. Verify PASS + `just lint`.

---

## Task 3: Extract `SandboxStatus` value object

**Objective:** Replace `status() -> dict[str, Any] | None` with a typed
`SandboxStatus` dataclass. The slash handler serialises it to JSON preserving
the keys the specdown `status-after` grep checks (`vm_id`, `provider`,
`public_ip`).

**Files:** `hermes_sandbox/lifecycle.py`, `hermes_sandbox/__init__.py`,
`tests/test_lifecycle.py`

**Step 1 — failing test:**
```python
def test_status_returns_sandboxstatus(tmp_path, fake_key_factory):
    from hermes_sandbox.lifecycle import SandboxStatus
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    st = mgr.status()
    assert isinstance(st, SandboxStatus)
    assert st.vm_id
    assert st.provider == "fake"
    assert st.public_ip
```

**Step 2 — implement** `SandboxStatus` dataclass with fields matching the state
file (`vm_id, provider, name, region, public_ip, public_ipv6, ssh_host,
ssh_user, created_at, last_used`) plus live-probe fields (`live_status, live_ip,
alive, error`). `status()` builds and returns it (or None). `_HELP`/slash
handler: `json.dumps(asdict(st), indent=2, default=str)`.

**Step 3 — verify** the spec keys survive: run the fake-provider specdown
`status-after` block mentally — `vm_id`/`provider`/`public_ip` present. `just
test` + `just lint`.

---

## Task 4: Move provider defaults onto `CloudProvider`

**Objective:** Remove the `if provider_name == "digitalocean"` branch from
`SandboxConfig.from_env`. Each `CloudProvider` declares `default_region`,
`default_image`, `default_instance_type` class attrs; `from_env` reads them via
`get_provider_class`. Fallback to base defaults if not registered.

**Files:** `hermes_sandbox/provider.py`, `hermes_sandbox/hetzner.py`,
`hermes_sandbox/digitalocean.py`, `hermes_sandbox/lifecycle.py`

**Step 1 — test** (already green, keep): `test_config_from_env_digitalocean_defaults`
asserts DO defaults. Add a test that a custom provider's defaults are honoured:
```python
def test_config_from_env_uses_provider_defaults(monkeypatch):
    # hetzner default region is nbg1
    monkeypatch.setenv("SANDBOX_PROVIDER", "hetzner")
    monkeypatch.setenv("HCLOUD_TOKEN", "t")
    cfg = SandboxConfig.from_env(__import__("os").environ)
    assert cfg.region == "nbg1"
    assert cfg.image == "ubuntu-24.04"
    assert cfg.instance_type == "cx23"
```

**Step 2 — implement:** add class attrs to `CloudProvider` (base = hetzner
values), override on `HetznerProvider`/`DigitalOceanProvider`. Rewrite
`from_env` to look them up. Verify existing DO-defaults test still passes + new
test passes + `just lint`.

---

## Task 5: Extract `poll_until` helper + injectable sleep into providers

**Objective:** DRY the two provider poll loops; make sleep injectable so the
loops are testable without real sleeping.

**Files:** `hermes_sandbox/provider.py` (add `poll_until`), `hermes_sandbox/hetzner.py`,
`hermes_sandbox/digitalocean.py`

**Step 1 — failing test** for `poll_until`:
```python
def test_poll_until_returns_when_predicate_true():
    from hermes_sandbox.provider import poll_until
    calls = {"n": 0}
    def pred():
        calls["n"] += 1
        return calls["n"] >= 3
    slept = []
    poll_until(pred, interval=0, timeout=5, sleep=slept.append)
    assert calls["n"] == 3
    assert len(slept) == 2
```
Add a timeout test too.

**Step 2 — implement** `poll_until(predicate, *, interval, timeout, sleep=time.sleep) ->
None` that raises `TimeoutError` if the deadline passes. Refactor both
providers' `provision` to use it, accepting `sleep` in `__init__` (default
`time.sleep`). Keep `poll_interval=0` tests green.

**Step 3 — verify** `just test` + `just lint`.

---

## Task 6: Freeze `VMInstance`, use `dataclasses.replace`

**Objective:** Immutability for the domain entity.

**Files:** `hermes_sandbox/provider.py`, `hermes_sandbox/digitalocean.py`,
`tests/test_lifecycle.py`, `tests/test_providers_api.py`

**Step 1 — test:** add
```python
def test_vminstance_is_frozen():
    from hermes_sandbox.provider import VMInstance
    vm = VMInstance("1","n","running","1.2.3.4","r","p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        vm.status = "off"
```
Run → FAIL.

**Step 2 — implement:** `@dataclass(frozen=True)` on `VMInstance`. Change DO
`_to_vm_running_aware` to `dataclasses.replace(vm, status="running")`. Update
`test_state_records_ipv6_and_ssh_host` to use `replace` instead of mutating.
Verify `just test` + `just lint` (mypy frozen dataclass fine).

---

## Task 7: Fix `VMInstance.from_dict` dead fold logic

**Objective:** Remove the misleading comment + non-functional fold; keep a
correct, tested roundtrip that drops unknown keys.

**Files:** `hermes_sandbox/provider.py`, `tests/test_provider.py`

**Step 1 — test:** add a test asserting unknown keys are dropped (not folded):
```python
def test_vminstance_from_dict_drops_unknown_keys():
    vm = VMInstance.from_dict({"id":"1","name":"n","status":"running",
        "public_ip":"1.2.3.4","region":"r","provider":"p","bogus":"x"})
    assert vm.id == "1"
    assert vm.extra == {}
```
Run → check current behaviour; fix to match.

**Step 2 — implement** clean `from_dict`. Verify.

---

## Task 8: Narrow `_load_state` exception

**Objective:** Catch `(json.JSONDecodeError, OSError)` not bare `Exception`.

**Files:** `hermes_sandbox/lifecycle.py`

**Step 1 — test:** add a test that writes a corrupt state file and asserts
`_load_state` returns None (already covered indirectly; make explicit + add a
permissions test using a directory as the state path → OSError). Verify `just
test` + `just lint`.

---

## Task 9: Expose `ssh_env()` on `VMLifecycleManager`

**Objective:** Remove the `manager._load_state()` private access (`# noqa:
SLF001`) from `__init__._apply_ssh_env`.

**Files:** `hermes_sandbox/lifecycle.py`, `hermes_sandbox/__init__.py`

**Step 1 — test:**
```python
def test_ssh_env_returns_mapping(tmp_path, fake_key_factory):
    mgr = make_manager(tmp_path, fake_key_factory=fake_key_factory)
    mgr.ensure_vm()
    env = mgr.ssh_env()
    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_SSH_HOST"]
    assert env["TERMINAL_SSH_USER"] == "root"
```
Run → FAIL.

**Step 2 — implement** `ssh_env() -> dict[str, str] | None` on the manager
(encapsulates the `_load_state` + key path logic). Rewrite `_apply_ssh_env` to
call it and set `os.environ`. Remove `# noqa: SLF001`. Verify `just test` +
`just lint` (no SLF001).

---

## Task 10: Stop misusing `chmod_private_key` on the state file

**Objective:** `_save_state` calls `chmod_private_key(state_path)` on a JSON
file; the helper's docstring says "Ensure a private key is 0600". Inline a clear
`os.chmod(self.state_path, 0o600)` with a comment, or add a generic
`restrict_to_owner(path)` in `ssh_util.py`.

**Files:** `hermes_sandbox/ssh_util.py`, `hermes_sandbox/lifecycle.py`

**Step 1 — test:** `test_save_state_restricts_permissions` asserting the state
file mode is 0o600 after `_save_state`. Run → should pass already (current code
does chmod); keep as a regression guard. Then refactor to the generic helper.
Verify `just test` + `just lint`.

---

## Final gate

`just lint && just test` green. Optionally `just specdown` (fake provider always
runs). Coverage should not drop below 75%.
