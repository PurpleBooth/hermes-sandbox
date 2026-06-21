# CLI Reference

Every `hermes vm-sandbox` subcommand, its output, and its exit code.

The CLI is available once the plugin is enabled (`hermes plugins enable
vm-sandbox`).  All subcommands also work as the in-session slash command
`/vm-sandbox <subcommand>`.

## Available providers

List every registered cloud provider.

```shell,script(name="available", expected_exit_code=0)
hermes vm-sandbox available
```

```text,verify(script_name="available", stream=stdout)
Registered providers: digitalocean, fake, hetzner
```

## Status

Show the current sandbox VM.  When no VM has been provisioned yet:

```shell,script(name="status-none", expected_exit_code=0)
VM_SANDBOX_PROVIDER=fake hermes vm-sandbox status
```

```text,verify(script_name="status-none", stream=stdout)
[vm-sandbox] No sandbox VM recorded.
```

When the plugin is not configured (no provider / token set), it tells you
how to fix that:

```shell,script(name="status-unconfigured", expected_exit_code=0)
hermes vm-sandbox status
```

```text,verify(script_name="status-unconfigured", stream=stdout)
[vm-sandbox] Not configured. Set VM_SANDBOX_PROVIDER and a cloud token (e.g. HCLOUD_TOKEN) in ~/.hermes/.env, then run `hermes vm-sandbox enable`.
```

## Provision

Provision (or reuse) a sandbox VM.  Returns the VM id, IP, and provider.

```shell,script(name="provision", expected_exit_code=0)
VM_SANDBOX_PROVIDER=fake hermes vm-sandbox provision
```

```text,verify(script_name="provision", stream=stdout)
[vm-sandbox] VM ready: fake-1 @ 10.0.0.1 (fake)
```

## Teardown

Destroy the recorded sandbox VM.

```shell,script(name="teardown", expected_exit_code=0)
VM_SANDBOX_PROVIDER=fake hermes vm-sandbox teardown
```

```text,verify(script_name="teardown", stream=stdout)
[vm-sandbox] VM torn down.
```

If there is no VM to tear down, it says so:

```shell,script(name="teardown-none", expected_exit_code=0)
VM_SANDBOX_PROVIDER=fake hermes vm-sandbox teardown
```

```text,verify(script_name="teardown-none", stream=stdout)
[vm-sandbox] No VM to tear down.
```

## Enable

Set `terminal.backend=ssh` in config.yaml so Hermes' ssh terminal backend is
the stable default.  Run this once during initial setup.

```shell,script(name="enable", expected_exit_code=0)
hermes vm-sandbox enable | grep '^\[vm-sandbox\]'
```

```text,verify(script_name="enable", stream=stdout)
[vm-sandbox] Set terminal.backend=ssh. Restart Hermes to apply.
```
