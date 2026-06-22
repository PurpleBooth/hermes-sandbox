# CLI Reference

Every `hermes sandbox` subcommand, its output, and its exit code.

The CLI is available once the plugin is enabled (`hermes plugins enable
sandbox`).  All subcommands also work as the in-session slash command
`/sandbox <subcommand>`.

## Available providers

List every registered cloud provider.

```shell,script(name="available", expected_exit_code=0)
hermes sandbox available
```

```text,verify(script_name="available", stream=stdout)
Registered providers: digitalocean, dry-run, hetzner, qemu
```

## Status

Show the current sandbox VM.  When no VM has been provisioned yet:

```shell,script(name="status-none", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox status
```

```text,verify(script_name="status-none", stream=stdout)
[sandbox] No sandbox VM recorded.
```

When the plugin is not configured (no provider / token set), it tells you
how to fix that:

```shell,script(name="status-unconfigured", expected_exit_code=0)
hermes sandbox status
```

```text,verify(script_name="status-unconfigured", stream=stdout)
[sandbox] Not configured. Set SANDBOX_PROVIDER and a cloud token (e.g. HCLOUD_TOKEN) in ~/.hermes/.env, then run `hermes sandbox enable`.
```

## Provision

Provision (or reuse) a sandbox VM.  Returns the VM id, IP, and provider.

```shell,script(name="provision", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox provision | sed -E 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/UUID/g; s/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/IP/g'
```

```text,verify(script_name="provision", stream=stdout)
[sandbox] VM ready: UUID @ IP (dry-run)
```

## Teardown

Destroy the recorded sandbox VM.

```shell,script(name="teardown", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox teardown
```

```text,verify(script_name="teardown", stream=stdout)
[sandbox] VM torn down.
```

If there is no VM to tear down, it says so:

```shell,script(name="teardown-none", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox teardown
```

```text,verify(script_name="teardown-none", stream=stdout)
[sandbox] No VM to tear down.
```

## List

List all sandbox VMs on the cloud provider.  This queries the cloud API
for VMs tagged `hermes-sandbox`, so it works across sessions and processes.

```shell,script(name="list-none", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox list
```

```text,verify(script_name="list-none", stream=stdout)
[sandbox] No sandbox VMs found.
```

## Recreate

Destroy the current VM and provision a fresh one.  Use this when a VM is
broken or unreachable — it tears down the old VM and creates a new one in
its place.

```shell,script(name="recreate", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox recreate | sed -E 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/UUID/g; s/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/IP/g'
```

```text,verify(script_name="recreate", stream=stdout)
[sandbox] VM ready: UUID @ IP (dry-run)
```

Clean up the VM created by recreate so it doesn't leak into other specs:

```shell,script(name="recreate-cleanup", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox teardown
```

```text,verify(script_name="recreate-cleanup", stream=stdout)
[sandbox] VM torn down.
```

## Cleanup

Tear down stray VMs — VMs on the cloud that aren't tracked by any local
state file.  This happens when a session ends abnormally (crash, kill)
and the `on_session_end` hook doesn't run.

```shell,script(name="cleanup-none", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox cleanup
```

```text,verify(script_name="cleanup-none", stream=stdout)
[sandbox] No stray VMs found.
```

## Enable

Set `terminal.backend=ssh` in config.yaml so Hermes' ssh terminal backend is
the stable default.  Run this once during initial setup.

```shell,script(name="enable", expected_exit_code=0)
hermes sandbox enable | grep '^\[sandbox\]'
```

```text,verify(script_name="enable", stream=stdout)
[sandbox] Set terminal.backend=ssh. Restart Hermes to apply.
```
