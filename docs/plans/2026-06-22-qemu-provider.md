# QEMU Local Provider

## Overview
Add a `qemu` provider that runs VMs locally via `qemu-system-x86_64`. Unlike
cloud providers (Hetzner, DigitalOcean), QEMU is a local hypervisor: no REST
API, no bearer token, no cloud network. SSH is via localhost with a
host-forwarded port.

## Design

### Provider interface mapping
- `provision()`: start `qemu-system-x86_64 -daemonize` with the disk image,
  SSH key injected via cloud-init, and host-forwarded SSH port. Return a
  VMInstance with `public_ip="127.0.0.1"`, `ssh_port` in `extra`.
- `get()`: return the in-memory VM record (status="running").
- `teardown()`: kill the QEMU process, remove from in-memory tracking.
  Idempotent (missing VM = no-op).
- `list_by_label()`: return in-memory VMs matching the label.

### Instance type
QEMU uses `<ncpu>-<mem>gb` format (e.g. `2cpu-4gb` -> `-smp 2 -m 4096`).
Default: `2cpu-4gb`.

### SSH port
QEMU user-mode networking forwards host port -> guest 22. Start at 2222,
increment per VM. Hermes' ssh backend already supports `TERMINAL_SSH_PORT`.

### Subprocess injection
A `runner: Callable[[list[str]], CompletedProcess]` is injectable for tests
(analogous to `request_fn` for REST providers). Production uses
`subprocess.run`.

### Token
No token needed. `is_configured()` treats `qemu` as token-free (like
`dry-run`).

## Changes

1. `state.py`: Add `ssh_port: int = 22` to `SandboxState`; `for_vm` reads
   it from `vm.extra.get("ssh_port", 22)`.
2. `lifecycle.py`: `ssh_env()` emits `TERMINAL_SSH_PORT` when port != 22.
3. `config.py`: `is_configured()` treats `qemu` as token-free.
4. `qemu.py`: New `QemuProvider`.
5. `__init__.py`: Import `qemu` for registration; update docstring/help.
6. `tests/test_provider_contract.py`: Add QEMU to factory.
7. `tests/test_qemu.py`: QEMU-specific tests (TDD).
8. Docs: cli.md (available output), configuration.md (qemu row).

## Execution order (single commit)
1. TDD: write tests first (test_qemu.py + contract factory)
2. Implement state.py + lifecycle.py + config.py changes
3. Implement qemu.py
4. Wire into __init__.py
5. Update docs
6. Full gate: just check
