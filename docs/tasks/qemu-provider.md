# Task: Sandbox VM with the Local QEMU Provider

The `qemu` provider runs a VM locally on your own machine via
`qemu-system-x86_64` — no cloud account, no API token, no network egress.
SSH is via localhost with a host-forwarded port.  Use it when you want a
real sandbox VM without paying for cloud time.

You need `qemu-system-x86_64` and an ISO builder (`xorriso`, `genisoimage`,
or `mkisofs`) installed.
When `SANDBOX_IMAGE` is unset, the provider downloads the Ubuntu 24.04
cloud image (~600 MB) to a local cache on first use and reuses it
thereafter.

## Check there is no VM yet

Before you start, there is no sandbox VM:

```shell,script(name="status-before", expected_exit_code=0)
SANDBOX_PROVIDER=qemu hermes sandbox status
```

```text,verify(script_name="status-before", stream=stdout)
[sandbox] No sandbox VM recorded.
```

## Provision the VM

This boots a real QEMU process.  The first run downloads the Ubuntu cloud
image, so it can take a few minutes; subsequent runs reuse the cache.

```shell,script(name="provision", expected_exit_code=0)
SANDBOX_PROVIDER=qemu hermes sandbox provision
```

## Check the VM status

After provisioning, `status` shows the VM details.  The provider is `qemu`
and the public IP is localhost:

```shell,script(name="status-after", expected_exit_code=0)
SANDBOX_PROVIDER=qemu hermes sandbox status | grep '"provider"'
```

```text,verify(script_name="status-after", stream=stdout)
  "provider": "qemu",
```

## Tear down the VM

When you are done, destroy the VM and its QEMU process:

```shell,script(name="teardown", expected_exit_code=0)
SANDBOX_PROVIDER=qemu hermes sandbox teardown
```

```text,verify(script_name="teardown", stream=stdout)
[sandbox] VM torn down.
```

## Confirm it is gone

```shell,script(name="status-after-teardown", expected_exit_code=0)
SANDBOX_PROVIDER=qemu hermes sandbox status
```

```text,verify(script_name="status-after-teardown", stream=stdout)
[sandbox] No sandbox VM recorded.
```
