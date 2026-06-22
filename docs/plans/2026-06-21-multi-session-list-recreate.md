# Multi-Session, List, and Recreate Features

## Feature 1: Multiple concurrent sessions

### Current state
- One VM, one state file (`~/.hermes/sandbox/state.json`), one singleton manager.
- `on_session_start` receives `session_id` but ignores it.
- `on_session_end` auto-tears-down the single VM if configured.

### Design
Add `SANDBOX_MODE` env var (shared | isolated, default shared).

**shared mode (default, backward-compatible):**
- One VM for all sessions. Single state file at `~/.hermes/sandbox/state.json`.
- `on_session_start`: reuse/provision the shared VM, set `TERMINAL_SSH_*`.
- `on_session_end`: auto-teardown as before (first session to end tears down
  the shared VM — documented limitation; use isolated mode for per-session
  isolation).
- Terminal cache invalidated with key `"default"` (current behaviour).

**isolated mode:**
- One VM per session. State at `~/.hermes/sandbox/sessions/<session_id>/state.json`.
- `on_session_start`: create a per-session `VMLifecycleManager` with its own
  `JsonStateRepository`, provision, set `TERMINAL_SSH_*`.
- `on_session_end`: tear down the session's VM (if auto-teardown configured),
  then drop the manager from the session registry.
- Terminal cache invalidated with key `session_id` (so each session's SSH
  connection is managed independently).
- A `_sessions: dict[str, VMLifecycleManager]` registry replaces the singleton
  for isolated mode.

**Changes:**
1. `config.py`: add `mode: str = "shared"` field, read `SANDBOX_MODE`.
2. `__init__.py`: refactor hooks to dispatch on mode. In isolated mode, build
   per-session managers. Use `session_id` for cache invalidation key.
3. `lifecycle.py`: `VMLifecycleManager` already takes an injectable
   `state_repo` — no change needed; the hook layer just constructs a
   per-session repo.

### Tests (TDD)
- `test_config_from_env_mode`: SANDBOX_MODE=isolated sets `config.mode`.
- `test_on_session_start_isolated_creates_per_session_manager`: two sessions
  get different managers with different state files.
- `test_on_session_start_shared_uses_singleton`: two sessions share one manager.
- `test_on_session_end_isolated_tears_down_session_vm`: ending one session
  tears down its VM, not the other's.
- `test_on_session_end_shared_auto_teardown`: shared mode tears down as before.

---

## Feature 2: `list` command

### Design
`hermes sandbox list` / `/sandbox list`:
- Calls `provider.list_by_label(LABEL_KEY, LABEL_VALUE)` to find all VMs
  tagged `hermes-sandbox=true` on the cloud.
- Displays a table: id, name, provider, status, IP, region.
- If none: "[sandbox] No sandbox VMs found."

### Changes
1. `lifecycle.py`: add `list_vms() -> list[VMInstance]` to `VMLifecycleManager`.
2. `__init__.py`: add `list` subcommand to slash + CLI.
3. `docs/references/cli.md`: add `list` section.
4. `docs/tasks/dry-run-provider.md`: add a list step to the task spec.

### Tests (TDD)
- `test_list_vms_returns_provider_results`: manager.list_vms() delegates to
  provider.list_by_label.
- `test_list_vms_empty_when_no_vms`: returns empty list.
- `test_slash_list`: slash command output contains VM ids.
- `test_slash_list_empty`: "No sandbox VMs found."

---

## Feature 3: `recreate` command

### Design
`hermes sandbox recreate` / `/sandbox recreate`:
- Tears down the current VM (if any), clears state, provisions a fresh one.
- Returns the new VM details.
- If no current VM: same as `provision`.

### Changes
1. `lifecycle.py`: add `recreate() -> VMInstance` to `VMLifecycleManager`.
2. `__init__.py`: add `recreate` subcommand to slash + CLI.
3. `docs/references/cli.md`: add `recreate` section.

### Tests (TDD)
- `test_recreate_tears_down_and_provisions`: recreate destroys old VM, creates
  new one with a different id.
- `test_recreate_when_no_vm_just_provisions`: recreate with no state = provision.
- `test_slash_recreate`: slash command output contains "VM ready".

---

## Execution order (commit per feature)
1. Multi-session support (config + hooks + tests)
2. `list` command (manager method + slash/CLI + tests + specdown)
3. `recreate` command (manager method + slash/CLI + tests + specdown)
4. Final gate: just check + update skill
