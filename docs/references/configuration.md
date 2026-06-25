# Configuration Reference

The sandbox plugin is configured entirely through environment variables.
Put secrets in `~/.hermes/.env`; set non-secret defaults there or export
them in your shell.

## Provider selection

| Variable | Default | Values |
|---|---|---|
| `SANDBOX_PROVIDER` | `hetzner` | `hetzner` \| `digitalocean` \| `dry-run` \| `qemu` |

The `dry-run` provider is for testing — it provisions an in-memory VM and
requires no token.  The `qemu` provider runs VMs locally via QEMU — it
needs no cloud token but requires `qemu-system-x86_64` installed.  When
`SANDBOX_IMAGE` is unset, the QEMU provider automatically downloads the
Ubuntu 24.04 cloud image to a local cache on first use.

For step-by-step walkthroughs with each provider, see the task guides:
[Hetzner](../tasks/hetzner-provider.md),
[DigitalOcean](../tasks/digitalocean-provider.md),
[QEMU](../tasks/qemu-provider.md), and
[Dry-run](../tasks/dry-run-provider.md).

## Cloud credentials

| Variable | Provider | Notes |
|---|---|---|
| `HCLOUD_TOKEN` | hetzner | Hetzner Cloud API token |
| `SANDBOX_TOKEN` | any | Fallback for any provider |
| `DIGITALOCEAN_TOKEN` | digitalocean | DigitalOcean API token |
| `DO_API_TOKEN` | digitalocean | Alias for `DIGITALOCEAN_TOKEN` |

Only cloud providers require a token.  The `dry-run` and `qemu` providers
are always configured.

## VM settings

| Variable | Default (hetzner) | Default (digitalocean) | Default (qemu) | Default (dry-run) |
|---|---|---|---|---|
| `SANDBOX_INSTANCE_TYPE` | `cx23` | `s-2vcpu-4gb` | `2cpu-4gb` | `cx23` |
| `SANDBOX_REGION` | `nbg1` | `fra1` | `local` | `nbg1` |
| `SANDBOX_IMAGE` | `ubuntu-24.04` | `ubuntu-24-04-x64` | Ubuntu 24.04 cloud image (URL) | `ubuntu-24.04` |
| `SANDBOX_SSH_USER` | `hermes` | `hermes` | `hermes` | `hermes` |
| `SANDBOX_NAME` | `hermes-sandbox` | `hermes-sandbox` | `hermes-sandbox` | `hermes-sandbox` |

The `dry-run` provider reuses the base defaults (same as `hetzner`) since it
never talks to a real cloud.  For the `qemu` provider, `SANDBOX_REGION` is a
cosmetic label (always `local`) and `SANDBOX_IMAGE` defaults to the Ubuntu 24.04
server cloud image URL, which the provider downloads and caches on first use —
see the [QEMU task guide](../tasks/qemu-provider.md).

## Teardown

| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_AUTO_TEARDOWN_SECONDS` | `0` | `0` = keep VM across sessions; `>0` = tear down on session end |

## Environment forwarding

| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_FORWARD_ENV` | *(empty)* | Comma-separated env var names forwarded into the VM at provision time |

When `SANDBOX_FORWARD_ENV` is set, the named env vars are read from the
caller's process and injected into the VM via a cloud-init `write_files`
entry at `/etc/profile.d/sandbox-forwarded-env.sh`.  The file is owned by
`root:root` with `0600` permissions.  The login shell sources
`/etc/profile.d/*.sh` automatically, so the values appear in the session
snapshot on first connect.

Example: `SANDBOX_FORWARD_ENV=CODEBERG_TOKEN,GITHUB_TOKEN` forwards both
tokens into the VM so workers running `git clone` / `git push` against
GitHub and Codeberg authenticate correctly.

Values are set at provision time.  Rotating a token requires
`hermes sandbox recreate`.

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
`dry-run` or `qemu`.  You can check by running `hermes sandbox status`
(see the [CLI Reference](cli.md)):

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
