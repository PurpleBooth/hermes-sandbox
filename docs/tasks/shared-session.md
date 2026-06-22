# Task: Shared VM Across All Sessions

By default (`SANDBOX_MODE=shared`), all Hermes sessions reuse a single sandbox
VM.  The VM is provisioned once and kept warm — subsequent sessions find it
via the shared state file and reuse it rather than paying for a new provision.

This is the complement of [concurrent sessions](concurrent-sessions.md), where
each session gets its own VM.  Here, a second session's provision reuses the
existing VM and only one teardown is needed to clean up.  See the
[Configuration Reference](../references/configuration.md) for the full
`SANDBOX_MODE` documentation.

## Provision the shared VM

The first session provisions a VM:

```shell,script(name="provision", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run SANDBOX_MODE=shared hermes sandbox provision | sed -E 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/UUID/g; s/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/IP/g'
```

```text,verify(script_name="provision", stream=stdout)
[sandbox] VM ready: UUID @ IP (dry-run)
```

## Any session can see the VM

The shared state file means a new session finds the VM via `status`:

```shell,script(name="status", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run SANDBOX_MODE=shared hermes sandbox status | grep -E '"vm_id"|"provider"' | sed -E 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/UUID/g'
```

```text,verify(script_name="status", stream=stdout)
  "vm_id": "UUID",
  "provider": "dry-run",
```

## A second session reuses the VM

Another session starts and calls provision.  Because the VM is shared, it
reuses the existing one rather than creating a second VM:

```shell,script(name="provision-again", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run SANDBOX_MODE=shared hermes sandbox provision | sed -E 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/UUID/g; s/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/IP/g'
```

```text,verify(script_name="provision-again", stream=stdout)
[sandbox] VM ready: UUID @ IP (dry-run)
```

## One teardown removes the shared VM

A single teardown clears the VM for all sessions — there is only one:

```shell,script(name="teardown", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run SANDBOX_MODE=shared hermes sandbox teardown
```

```text,verify(script_name="teardown", stream=stdout)
[sandbox] VM torn down.
```

## Confirm it is gone

```shell,script(name="status-clean", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run SANDBOX_MODE=shared hermes sandbox status
```

```text,verify(script_name="status-clean", stream=stdout)
[sandbox] No sandbox VM recorded.
```
