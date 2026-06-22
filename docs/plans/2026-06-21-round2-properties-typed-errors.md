# sandbox Round 2: Property-Based Testing, Typed Errors, DRY Providers

> Iterative refinement on top of commit de7868d (round 1).

## Review findings (round 2)

### Property-based testing (NEW)
The codebase has rich *pure* domain logic tested only with single example
values. These are textbook PBT targets:
- `VMInstance` to_dict/from_dict roundtrip — should be identity for any value.
- `preferred_ip()` — always picks IPv6 when present, else IPv4, for any pair.
- `resolve_token()` — returns first non-empty from the ordered keys, for any
  env/keys combination.
- `SandboxConfig.from_env()` — an explicit SANDBOX_* override always wins
  over the provider default; token comes from the provider's env key.
- `poll_until()` — calls predicate ≥1 time; raises only after deadline; never
  sleeps after success.

Add `hypothesis` as a dev dependency and a `tests/test_properties.py`.

### Typed API errors (functional / robustness)
Both providers match teardown 404 with `if "HTTP 404" in str(exc) or
"not_found" in str(exc).lower()` — fragile string matching on a RuntimeError
message. Replace with a typed `ApiError(method, path, status_code, detail)`
exception raised in `_request`. Teardown then checks `exc.status_code == 404`
— no string parsing. This is the structured-error / monadic-adjacent pattern.

### DRY providers (best practices)
`hetzner.py` and `digitalocean.py` duplicate ~60 lines: identical `_request`,
identical `__init__` (token check, poll attrs, request_fn/sleep), near-identical
`_ensure_ssh_key`, identical teardown 404 logic. Extract a `RestCloudProvider`
base holding the shared HTTP plumbing + token/poll/sleep state + teardown
idempotency. Subclasses keep only their API-shape specifics (`_to_vm`,
provision body, endpoints, status normalisation).

### Functional status() (functional)
`status()` builds a `SandboxStatus` then mutates `st.live_status`/`st.live_ip`/
`st.alive`/`st.error`. Freeze `SandboxStatus` and build the final value with
`dataclasses.replace` (or construct once with a computed result), so there's no
in-place mutation.

### DDD layering (DDD)
`lifecycle.py` still fuses config (value object) + manager (use-case) + state
file I/O (infra). Extract a `StateRepository` Protocol in `provider.py` (domain)
and a `JsonStateRepository` (infra) so `VMLifecycleManager` depends on an
abstraction. Keep everything in the flat package (no subpackages — plugin
discovery risk). This is the smallest step toward layering.

## Execution (commit per step)

1. **chore: add hypothesis dev dep + tests/test_properties.py** (TDD: properties first)
2. **refactor: typed ApiError replaces RuntimeError string matching** (TDD)
3. **refactor: extract RestCloudProvider base, DRY hetzner/DO** (keep tests green)
4. **refactor: build SandboxStatus immutably in status()** (functional)
5. **refactor: StateRepository protocol for DDD layering** (keep tests green)
