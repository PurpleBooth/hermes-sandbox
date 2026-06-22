# Configuration Reference

The sandbox plugin is configured entirely through environment variables.
Put secrets in `~/.hermes/.env`; set non-secret defaults there or export
them in your shell.

## Provider selection

| Variable | Default | Values |
|---|---|---|
| `SANDBOX_PROVIDER` | `hetzner` | `hetzner` \| `digitalocean` \| `dry-run` |

The `dry-run` provider is for testing — it provisions an in-memory VM and
requires no token.

## Cloud credentials

| Variable | Provider | Notes |
|---|---|---|
| `HCLOUD_TOKEN` | hetzner | Hetzner Cloud API token |
| `SANDBOX_TOKEN` | any | Fallback for any provider |
| `DIGITALOCEAN_TOKEN` | digitalocean | DigitalOcean API token |
| `DO_API_TOKEN` | digitalocean | Alias for `DIGITALOCEAN_TOKEN` |

Only real providers require a token.  The `dry-run` provider is always
configured.

## VM settings

| Variable | Default (hetzner) | Default (digitalocean) | Default (dry-run) |
|---|---|---|---|
| `SANDBOX_INSTANCE_TYPE` | `cx23` | `s-2vcpu-4gb` | `cx23` |
| `SANDBOX_REGION` | `nbg1` | `fra1` | `nbg1` |
| `SANDBOX_IMAGE` | `ubuntu-24.04` | `ubuntu-24-04-x64` | `ubuntu-24.04` |
| `SANDBOX_SSH_USER` | `root` | `root` | `root` |
| `SANDBOX_NAME` | `hermes-sandbox` | `hermes-sandbox` | `hermes-sandbox` |

## Teardown

| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_AUTO_TEARDOWN_SECONDS` | `0` | `0` = keep VM across sessions; `>0` = tear down on session end |

## Session mode

| Variable | Default | Values |
|---|---|---|
| `SANDBOX_MODE` | `shared` | `shared` = one VM for all sessions; `isolated` = one VM per session |

In `shared` mode (the default), all sessions reuse a single VM.  The VM
state lives at `$HERMES_HOME/sandbox/state.json`.

In `isolated` mode, each session provisions its own VM.  The per-session
state lives at `$HERMES_HOME/sandbox/sessions/<session_id>/state.json`.
When a session ends, its VM is torn down if `SANDBOX_AUTO_TEARDOWN_SECONDS`
is greater than zero.

## State

The plugin records the current VM in `$HERMES_HOME/sandbox/state.json`
so a VM created in a previous session is reused.  The SSH keypair lives at
`$HERMES_HOME/sandbox/hermes-sandbox` (private, `0600`) and
`$HERMES_HOME/sandbox/hermes-sandbox.pub` (public).

## Verifying configuration

A provider is "configured" when it has a token, or when the provider is
`dry-run`.  You can check by running `hermes sandbox status`:

```shell,script(name="dry-run-configured", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox status
```

```text,verify(script_name="dry-run-configured", stream=stdout)
[sandbox] No sandbox VM recorded.
```

Without a provider or token, the plugin reports it is not configured:

```shell,script(name="unconfigured", expected_exit_code=0)
hermes sandbox status
```

```text,verify(script_name="unconfigured", stream=stdout)
[sandbox] Not configured. Set SANDBOX_PROVIDER and a cloud token (e.g. HCLOUD_TOKEN) in ~/.hermes/.env, then run `hermes sandbox enable`.
```
