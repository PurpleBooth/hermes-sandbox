# Task: Sandbox VM with the Dry-Run Provider

The `dry-run` provider simulates provisioning entirely in memory — no cloud
credentials required.  Use it to try the plugin, see how the lifecycle works,
and verify your setup: it goes through the exact same provision → status →
teardown flow as a real provider.

## Check there is no VM yet

Before you start, there is no sandbox VM:

```shell,script(name="status-before", expected_exit_code=0)
VM_SANDBOX_PROVIDER=dry-run hermes vm-sandbox status
```

```text,verify(script_name="status-before", stream=stdout)
[vm-sandbox] No sandbox VM recorded.
```

## Provision the VM

```shell,script(name="provision", expected_exit_code=0)
VM_SANDBOX_PROVIDER=dry-run hermes vm-sandbox provision
```

```text,verify(script_name="provision", stream=stdout)
[vm-sandbox] VM ready: dry-run-1 @ 10.0.0.1 (dry-run)
```

## Check the VM status

After provisioning, `status` shows the VM details.  The timestamps vary
between runs, so here we look at the stable fields:

```shell,script(name="status-after", expected_exit_code=0)
VM_SANDBOX_PROVIDER=dry-run hermes vm-sandbox status | grep -E '"vm_id"|"provider"|"public_ip"'
```

```text,verify(script_name="status-after", stream=stdout)
  "vm_id": "dry-run-1",
  "provider": "dry-run",
  "public_ip": "10.0.0.1",
```

## Tear down the VM

When you are done with the sandbox, destroy it:

```shell,script(name="teardown", expected_exit_code=0)
VM_SANDBOX_PROVIDER=dry-run hermes vm-sandbox teardown
```

```text,verify(script_name="teardown", stream=stdout)
[vm-sandbox] VM torn down.
```

## Confirm it is gone

```shell,script(name="status-after-teardown", expected_exit_code=0)
VM_SANDBOX_PROVIDER=dry-run hermes vm-sandbox status
```

```text,verify(script_name="status-after-teardown", stream=stdout)
[vm-sandbox] No sandbox VM recorded.
```
