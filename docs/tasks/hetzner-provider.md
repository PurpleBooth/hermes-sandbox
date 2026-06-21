# Task: Hetzner Provider Lifecycle

This task demonstrates the full provision → use → teardown cycle with
Hetzner Cloud.  It provisions a real server, verifies it is recorded,
and tears it down.

The `HCLOUD_TOKEN` environment variable must be set.  In CI it comes from
the `HETZNER_API_TOKEN` secret; locally it can be exported or set in
`~/.hermes/.env`.

## Verify the provider is registered

```shell,script(name="available", expected_exit_code=0)
hermes vm-sandbox available
```

```text,verify(script_name="available", stream=stdout)
Registered providers: digitalocean, fake, hetzner
```

## Start with no VM

```shell,script(name="status-before", expected_exit_code=0)
VM_SANDBOX_PROVIDER=hetzner hermes vm-sandbox status
```

```text,verify(script_name="status-before", stream=stdout)
[vm-sandbox] No sandbox VM recorded.
```

## Provision the VM

Provisions a real Hetzner server.  The id and IP are non-deterministic,
so we only check the exit code — the next step verifies the VM was
recorded.

```shell,script(name="provision", expected_exit_code=0)
VM_SANDBOX_PROVIDER=hetzner hermes vm-sandbox provision
```

## Verify the VM is recorded

After provisioning, `status` shows the VM.  We check the provider field,
which is stable.

```shell,script(name="status-after", expected_exit_code=0)
VM_SANDBOX_PROVIDER=hetzner hermes vm-sandbox status | grep '"provider"'
```

```text,verify(script_name="status-after", stream=stdout)
  "provider": "hetzner",
```

The state file at `$HERMES_HOME/vm-sandbox/state.json` contains the VM:

```shell,script(name="state-file", expected_exit_code=0)
test -f "$HERMES_HOME/vm-sandbox/state.json"
grep '"hetzner"' "$HERMES_HOME/vm-sandbox/state.json"
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
VM_SANDBOX_PROVIDER=hetzner hermes vm-sandbox teardown
```

```text,verify(script_name="teardown", stream=stdout)
[vm-sandbox] VM torn down.
```

## Verify the VM is gone

```shell,script(name="status-after-teardown", expected_exit_code=0)
VM_SANDBOX_PROVIDER=hetzner hermes vm-sandbox status
```

```text,verify(script_name="status-after-teardown", stream=stdout)
[vm-sandbox] No sandbox VM recorded.
```
