# sandbox

A Hermes Agent plugin that spins up a cloud VM as your terminal sandbox,
so Hermes runs shell commands, file edits, and `execute_code` inside an
isolated, ephemeral machine instead of on your host.

Want to do `docker build`, install root packages, or throw experiments at
a fresh box? This is the path.

## Install

    git clone https://codeberg.org/PurpleBooth/hermes-sandbox ~/.hermes/plugins/sandbox
    hermes plugins enable sandbox
    hermes sandbox enable

The `enable` command sets `terminal.backend=ssh` in config.yaml so Hermes'
ssh terminal backend is the stable default. Restart Hermes after install.

## Providers

| Provider | `SANDBOX_PROVIDER` | Token | Notes |
|---|---|---|---|
| Hetzner Cloud | `hetzner` | `HCLOUD_TOKEN` | Default. EU regions, IPv4+IPv6. |
| DigitalOcean | `digitalocean` | `DIGITALOCEAN_TOKEN` (or `DO_API_TOKEN`) | Global regions, IPv4+IPv6. |
| QEMU | `qemu` | none | Local hypervisor. Set `SANDBOX_IMAGE` to a qcow2 path. |
| Dry-run | `dry-run` | none | In-memory simulation for testing. |

List the providers available on your system:

```shell,script(name="readme-available", expected_exit_code=0)
hermes sandbox available
```

```text,verify(script_name="readme-available", stream=stdout)
Registered providers: digitalocean, dry-run, hetzner, qemu
```

## Configure

Put these in `~/.hermes/.env`:

    SANDBOX_PROVIDER=hetzner              # hetzner | digitalocean | dry-run | qemu
    HCLOUD_TOKEN=***                       # or DIGITALOCEAN_TOKEN / DO_API_TOKEN
    SANDBOX_INSTANCE_TYPE=cx23            # provider-specific
    SANDBOX_REGION=nbg1                   # provider-specific
    SANDBOX_IMAGE=ubuntu-24.04            # provider-specific
    SANDBOX_SSH_USER=root                 # default root (cloud images)
    SANDBOX_NAME=hermes-sandbox           # VM name
    SANDBOX_AUTO_TEARDOWN_SECONDS=0       # 0 = keep across sessions; >0 = teardown on session end
    SANDBOX_MODE=shared                   # shared = one VM for all; isolated = one VM per session

Only cloud providers need a token. The `dry-run` and `qemu` providers are
always configured.

## Check there is no VM yet

Before you start, there is no sandbox VM:

```shell,script(name="readme-status-none", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox status
```

```text,verify(script_name="readme-status-none", stream=stdout)
[sandbox] No sandbox VM recorded.
```

## Provision a VM

Provision (or reuse) a sandbox VM. The dry-run provider needs no token, so
you can try it right away:

```shell,script(name="readme-provision", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox provision | sed -E 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/UUID/g; s/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/IP/g'
```

```text,verify(script_name="readme-provision", stream=stdout)
[sandbox] VM ready: UUID @ IP (dry-run)
```

## Check the status

See the current sandbox VM:

```shell,script(name="readme-status", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox status | grep -E '"vm_id"|"provider"|"public_ip"' | sed -E 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/UUID/g; s/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/IP/g'
```

```text,verify(script_name="readme-status", stream=stdout)
  "vm_id": "UUID",
  "provider": "dry-run",
  "public_ip": "IP",
```

## List all sandbox VMs

Query the cloud for all VMs tagged `hermes-sandbox`:

```shell,script(name="readme-list", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox list
```

```text,verify(script_name="readme-list", stream=stdout)
[sandbox] No sandbox VMs found.
```

## Recreate a broken VM

If a VM is broken or unreachable, destroy it and provision a fresh one:

```shell,script(name="readme-recreate", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox recreate | sed -E 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/UUID/g; s/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/IP/g'
```

```text,verify(script_name="readme-recreate", stream=stdout)
[sandbox] VM ready: UUID @ IP (dry-run)
```

## Tear down

Destroy the sandbox VM when you are done:

```shell,script(name="readme-teardown", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox teardown
```

```text,verify(script_name="readme-teardown", stream=stdout)
[sandbox] VM torn down.
```

Confirm it is gone:

```shell,script(name="readme-status-gone", expected_exit_code=0)
SANDBOX_PROVIDER=dry-run hermes sandbox status
```

```text,verify(script_name="readme-status-gone", stream=stdout)
[sandbox] No sandbox VM recorded.
```

## Session commands

Inside a Hermes session, use the slash command:

    /sandbox status       # show the current sandbox VM
    /sandbox provision    # provision/reuse now
    /sandbox teardown     # destroy the VM
    /sandbox list         # list all sandbox VMs on the cloud
    /sandbox recreate     # destroy the current VM and provision a fresh one
    /sandbox cleanup      # tear down stray VMs not tracked by any session
    /sandbox enable       # set terminal.backend=ssh

From the shell:

    hermes sandbox status
    hermes sandbox available     # list registered providers

## Multiple sessions

Set `SANDBOX_MODE` to control how sessions share VMs:

- **`shared`** (default): one VM for all sessions. The VM is kept warm and
  reused. State lives at `~/.hermes/sandbox/state.json`.
- **`isolated`**: one VM per session. Each session provisions its own VM
  with state at `~/.hermes/sandbox/sessions/<session_id>/state.json`. When
  a session ends, its VM is torn down (if `SANDBOX_AUTO_TEARDOWN_SECONDS`
  is greater than zero).

## State

`~/.hermes/sandbox/state.json` records the current VM so a VM created in a
previous session is reused. The SSH keypair lives at
`~/.hermes/sandbox/hermes-sandbox` (private, `0600`) and
`~/.hermes/sandbox/hermes-sandbox.pub` (public).

## Develop

    just test          # pytest with coverage (unit + contract + property-based)
    just lint          # ruff + mypy strict
    just specdown      # end-to-end specs (including this README)
    just check         # full gate: lint + test + specdown

Tests use the in-memory `dry-run` provider and inject fake HTTP for the
Hetzner and DigitalOcean REST parsing — no cloud credentials required.
