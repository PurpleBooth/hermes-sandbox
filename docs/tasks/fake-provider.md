# Task: Fake Provider Lifecycle

The `fake` provider is an in-memory provider for testing.  It provisions a
VM instantly, requires no cloud credentials, and exercises the exact same
lifecycle manager and state file as the real providers.  This task
demonstrates the full provision → use → teardown cycle a user would run.

## Verify the fake provider is registered

```shell,script(name="available", expected_exit_code=0)
hermes vm-sandbox available
```

```text,verify(script_name="available", stream=stdout)
Registered providers: digitalocean, fake, hetzner
```

## Start with no VM

Before provisioning, there is no sandbox VM recorded.

```shell,script(name="status-before", expected_exit_code=0)
VM_SANDBOX_PROVIDER=fake hermes vm-sandbox status
```

```text,verify(script_name="status-before", stream=stdout)
[vm-sandbox] No sandbox VM recorded.
```

## Provision the VM

```shell,script(name="provision", expected_exit_code=0)
VM_SANDBOX_PROVIDER=fake hermes vm-sandbox provision
```

```text,verify(script_name="provision", stream=stdout)
[vm-sandbox] VM ready: fake-1 @ 10.0.0.1 (fake)
```

## Verify the VM is recorded

After provisioning, `status` shows the VM details.  The timestamps are
non-deterministic, so we check the stable fields with a JSON query.

```shell,script(name="status-after", expected_exit_code=0)
VM_SANDBOX_PROVIDER=fake hermes vm-sandbox status | grep -E '"vm_id"|"provider"|"public_ip"'
```

```text,verify(script_name="status-after", stream=stdout)
  "vm_id": "fake-1",
  "provider": "fake",
  "public_ip": "10.0.0.1",
```

## Run something on the VM

The state file at `$HERMES_HOME/vm-sandbox/state.json` records the VM so it
is reused across invocations.  In a real session, Hermes' ssh terminal
backend would route shell commands into the VM.  Here we verify the state
file is present and contains the provisioned VM.

```shell,script(name="state-file", expected_exit_code=0)
test -f "$HERMES_HOME/vm-sandbox/state.json"
grep '"fake-1"' "$HERMES_HOME/vm-sandbox/state.json"
```

```text,verify(script_name="state-file", stream=stdout)
  "vm_id": "fake-1",
```

The SSH keypair was generated during provisioning:

```shell,script(name="keypair", expected_exit_code=0)
test -f "$HERMES_HOME/vm-sandbox/hermes-vm-sandbox"
test -f "$HERMES_HOME/vm-sandbox/hermes-vm-sandbox.pub"
echo "keypair exists"
```

```text,verify(script_name="keypair", stream=stdout)
keypair exists
```

## Tear down the VM

```shell,script(name="teardown", expected_exit_code=0)
VM_SANDBOX_PROVIDER=fake hermes vm-sandbox teardown
```

```text,verify(script_name="teardown", stream=stdout)
[vm-sandbox] VM torn down.
```

## Verify the VM is gone

After teardown, no sandbox VM is recorded.

```shell,script(name="status-after-teardown", expected_exit_code=0)
VM_SANDBOX_PROVIDER=fake hermes vm-sandbox status
```

```text,verify(script_name="status-after-teardown", stream=stdout)
[vm-sandbox] No sandbox VM recorded.
```
