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
caller's process and appended into the VM's `/etc/environment` via a
cloud-init `runcmd` entry (append, not clobber).  PAM (`pam_env`) reads
`/etc/environment` during every SSH session, so the values are available
in the process environment without any shell sourcing or snapshot
dependency.

Example: `SANDBOX_FORWARD_ENV=CODEBERG_TOKEN,GITHUB_TOKEN` forwards both
tokens into the VM so workers running `git clone` / `git push` against
GitHub and Codeberg authenticate correctly.

Values are set at provision time.  Rotating a token requires
`hermes sandbox recreate`.

## Netrc credentials

| Variable | Default | Meaning |
|---|---|---|
| `SANDBOX_NETRC` | *(empty)* | Comma-separated machine specs written to `/home/hermes/.netrc` |

Prefer this over `SANDBOX_FORWARD_ENV` for **git HTTPS authentication**.
curl and git read `~/.netrc` automatically, so tokens do not need to live
in the process environment.

Each entry is one of:

* `machine:PASSWORD_ENV` — login is chosen from host defaults
  (`x-access-token` for `github.com` / `api.github.com`; `oauth2` otherwise)
* `machine:login:PASSWORD_ENV` — explicit login name

Example:

```bash
# in ~/.hermes/.env
GITHUB_TOKEN=ghp_...
CODEBERG_TOKEN=...
SANDBOX_NETRC=github.com:GITHUB_TOKEN,codeberg.org:CODEBERG_TOKEN
```

This produces `/home/hermes/.netrc` at provision time:

```netrc
machine github.com
login x-access-token
password ghp_...

machine codeberg.org
login oauth2
password ...
```

The file is owned by `hermes:hermes` with mode `0600`.  Specs whose
password env var is missing or empty are skipped.  Rotating a token
requires `hermes sandbox recreate`.

Full-form example with an explicit login:

```bash
SANDBOX_NETRC=gitlab.com:oauth2:GITLAB_TOKEN
```

## Package and setup customisation

| Variable | Default | Format | Purpose |
|---|---|---|---|
| `SANDBOX_APT_REPOS` | *(empty)* | Newline-separated `deb ...` lines | Additional apt repositories injected as `apt_sources` |
| `SANDBOX_PACKAGES` | *(empty)* | Comma-separated package names | Extra apt packages appended to the `packages` list |
| `SANDBOX_SETUP_CMDS` | *(empty)* | Newline-separated shell commands | Custom `runcmd` entries appended after the existing ones |

These let you customise the VM at provisioning time without modifying the
plugin.  All three are additive — the base package list and `runcmd`
entries installed by the plugin remain unchanged.  Values are written into
the cloud-config YAML, not evaluated by a shell, so there is no shell
injection risk.

`SANDBOX_APT_REPOS` must contain one repository per line.  Each line is
injected as a cloud-init `apt_sources` entry *before* the `packages`
section, so packages from those repositories are available for
installation:

    SANDBOX_APT_REPOS=deb https://packages.example.com stable main

`SANDBOX_PACKAGES` is a comma-separated list of additional apt packages
that are appended to the existing `packages` list:

    SANDBOX_PACKAGES=htop,vim,tmux

`SANDBOX_SETUP_CMDS` contains one shell command per line, appended to the
`runcmd` section after the plugin's built-in setup commands:

    SANDBOX_SETUP_CMDS=echo custom-setup-done
    curl -fsSL https://example.com/install.sh | sh

All three can be combined in a single provision.  Changes take effect on
the next provision — use `hermes sandbox recreate` to apply them to an
existing VM.

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
