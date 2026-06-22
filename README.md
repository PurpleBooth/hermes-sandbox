# sandbox

A Hermes Agent plugin that spins up a **cloud VM** as the terminal sandbox,
so Hermes runs shell commands, file edits, and `execute_code` inside an
isolated, ephemeral machine instead of on your host or in a container.
Want to do `docker build`, install root packages, or throw experiments at a
fresh box? This is the path.

## How it works

1. On `on_session_start` the plugin provisions (or reuses) a VM on your
   configured cloud provider.
2. It writes an ed25519 SSH keypair to `~/.hermes/sandbox/`.
3. It sets `TERMINAL_ENV=ssh` and the `TERMINAL_SSH_*` env vars so Hermes'
   built-in **ssh** terminal backend routes every `terminal`, `file`, and
   `execute_code` call into the VM.
4. On `on_session_end` the VM is torn down (only if `SANDBOX_AUTO_TEARDOWN_SECONDS>0`);
   by default the VM is kept warm and reused next session.

The plugin orchestrates Hermes' existing `ssh` backend — no core changes,
no monkeypatching.

## Providers

| Provider | `SANDBOX_PROVIDER` | Token env var | Default region | Default size |
|---|---|---|---|---|
| Hetzner Cloud | `hetzner` | `HCLOUD_TOKEN` | `nbg1` | `cx23` |
| DigitalOcean | `digitalocean` | `DIGITALOCEAN_TOKEN` (or `DO_API_TOKEN`) | `fra1` | `s-2vcpu-4gb` |

Both providers enable IPv4 **and** IPv6; SSH prefers the IPv6 address when
present. Adding a provider is one file (`<name>.py`) + the `@register_provider`
decorator.

## Install

```bash
# 1. Clone the plugin into your Hermes plugins dir.
git clone https://codeberg.org/PurpleBooth/hermes-sandbox ~/.hermes/plugins/sandbox

# 2. Enable it.
hermes plugins enable sandbox

# 3. Set terminal.backend=ssh so the ssh backend is the stable default.
hermes sandbox enable     # or: hermes config set terminal.backend ssh
```

## Configure (in `~/.hermes/.env`)

```bash
SANDBOX_PROVIDER=hetzner              # hetzner | digitalocean | dry-run
HCLOUD_TOKEN=...                         # or DIGITALOCEAN_TOKEN / DO_API_TOKEN
SANDBOX_INSTANCE_TYPE=cx23            # provider-specific
SANDBOX_REGION=nbg1                   # provider-specific
SANDBOX_IMAGE=ubuntu-24.04            # provider-specific
SANDBOX_SSH_USER=root                 # default root (cloud images)
SANDBOX_NAME=hermes-sandbox           # VM name
SANDBOX_AUTO_TEARDOWN_SECONDS=0       # 0 = keep across sessions; >0 = teardown on session end
```

Restart Hermes after first configuration.

## Use

In a session:

```
/sandbox status       # show the current sandbox VM
/sandbox provision    # provision/reuse now
/sandbox teardown     # destroy the VM
/sandbox enable       # set terminal.backend=ssh
```

From the shell:

```
hermes sandbox status
hermes sandbox available     # list registered providers
```

## State

`~/.hermes/sandbox/state.json` records the current VM so a VM created in a
previous session is reused. The SSH keypair lives at
`~/.hermes/sandbox/hermes-sandbox` (`0600`).

## Develop

```bash
just test          # pytest with coverage (unit + contract + property-based)
just lint          # ruff + mypy strict
just specdown      # end-to-end specs via the test-harness wrapper
```

Tests use an in-memory `dry-run` provider and inject fake HTTP for the
Hetzner/DO REST parsing — no cloud credentials required.

## Architecture notes

- The env-var race (config→env re-bridging) is avoided by setting
  `terminal.backend: ssh` in `config.yaml` once (`/sandbox enable`); the
  plugin only fills in `TERMINAL_SSH_*` at runtime after provisioning.
- The plugin is fail-open: if provisioning fails, the session continues with
  the host's existing terminal backend. We never crash a user's session
  because the sandbox couldn't boot.
- IPv6 is preferred for SSH where assigned; falls back to IPv4.
