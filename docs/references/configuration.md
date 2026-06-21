# Configuration Reference

The vm-sandbox plugin is configured entirely through environment variables.
Put secrets in `~/.hermes/.env`; set non-secret defaults there or export
them in your shell.

## Provider selection

| Variable | Default | Values |
|---|---|---|
| `VM_SANDBOX_PROVIDER` | `hetzner` | `hetzner` \| `digitalocean` \| `fake` |

The `fake` provider is for testing — it provisions an in-memory VM and
requires no token.

## Cloud credentials

| Variable | Provider | Notes |
|---|---|---|
| `HCLOUD_TOKEN` | hetzner | Hetzner Cloud API token |
| `VM_SANDBOX_TOKEN` | any | Fallback for any provider |
| `DIGITALOCEAN_TOKEN` | digitalocean | DigitalOcean API token |
| `DO_API_TOKEN` | digitalocean | Alias for `DIGITALOCEAN_TOKEN` |

Only real providers require a token.  The `fake` provider is always
configured.

## VM settings

| Variable | Default (hetzner) | Default (digitalocean) | Default (fake) |
|---|---|---|---|
| `VM_SANDBOX_INSTANCE_TYPE` | `cx23` | `s-2vcpu-4gb` | `cx23` |
| `VM_SANDBOX_REGION` | `nbg1` | `fra1` | `nbg1` |
| `VM_SANDBOX_IMAGE` | `ubuntu-24.04` | `ubuntu-24-04-x64` | `ubuntu-24.04` |
| `VM_SANDBOX_SSH_USER` | `root` | `root` | `root` |
| `VM_SANDBOX_NAME` | `hermes-sandbox` | `hermes-sandbox` | `hermes-sandbox` |

## Teardown

| Variable | Default | Meaning |
|---|---|---|
| `VM_SANDBOX_AUTO_TEARDOWN_SECONDS` | `0` | `0` = keep VM across sessions; `>0` = tear down on session end |

## State

The plugin records the current VM in `$HERMES_HOME/vm-sandbox/state.json`
so a VM created in a previous session is reused.  The SSH keypair lives at
`$HERMES_HOME/vm-sandbox/hermes-vm-sandbox` (private, `0600`) and
`$HERMES_HOME/vm-sandbox/hermes-vm-sandbox.pub` (public).

## Verifying configuration

A provider is "configured" when it has a token, or when the provider is
`fake`.  You can check by running `hermes vm-sandbox status`:

```shell,script(name="fake-configured", expected_exit_code=0)
VM_SANDBOX_PROVIDER=fake hermes vm-sandbox status
```

```text,verify(script_name="fake-configured", stream=stdout)
[vm-sandbox] No sandbox VM recorded.
```

Without a provider or token, the plugin reports it is not configured:

```shell,script(name="unconfigured", expected_exit_code=0)
hermes vm-sandbox status
```

```text,verify(script_name="unconfigured", stream=stdout)
[vm-sandbox] Not configured. Set VM_SANDBOX_PROVIDER and a cloud token (e.g. HCLOUD_TOKEN) in ~/.hermes/.env, then run `hermes vm-sandbox enable`.
```
