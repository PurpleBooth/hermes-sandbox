# Task: Concurrent Sessions Don't Interfere

Each Hermes session gets its own sandbox VM — provisioning in one session
must never reuse or clobber a VM from another.  This spec verifies the
isolation by running two provision cycles back-to-back with the dry-run
provider and confirming each session gets a completely distinct VM.

## Session A: provision a VM

Provision a VM for the first session and capture its ID:

```shell,script(name="provision-a", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox provision \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
  | head -1 > session-a-vm.txt
```

## Session A ends: tear down

```shell,script(name="teardown-a", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox teardown
```

```text,verify(script_name="teardown-a", stream=stdout)
[sandbox] VM torn down.
```

## Session B: provision a fresh VM

A second session starts and provisions its own VM:

```shell,script(name="provision-b", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox provision \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
  | head -1 > session-b-vm.txt
```

## The two sessions got different VMs

Session B must not have inherited session A's VM — the IDs differ:

```shell,script(name="distinct-ids", expected_exit_code=0)
test "$(cat session-a-vm.txt)" != "$(cat session-b-vm.txt)" \
  && echo "sessions isolated: distinct VM IDs confirmed"
```

```text,verify(script_name="distinct-ids", stream=stdout)
sessions isolated: distinct VM IDs confirmed
```

## Clean up session B

```shell,script(name="teardown-b", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox teardown
```

```text,verify(script_name="teardown-b", stream=stdout)
[sandbox] VM torn down.
```

## Confirm no state leaked

```shell,script(name="status-clean", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox status
```

```text,verify(script_name="status-clean", stream=stdout)
[sandbox] No sandbox VM recorded.
```
