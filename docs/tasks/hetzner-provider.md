# Task: Sandbox VM with Hetzner Cloud

Provision a real Hetzner Cloud server as your Hermes sandbox, run commands
on it, then tear it down when you are done.

Set `SANDBOX_PROVIDER=hetzner` and `HCLOUD_TOKEN` in `~/.hermes/.env`
(or export them in your shell).  In CI the token comes from the
`HETZNER_API_TOKEN` secret.  See the
[Configuration Reference](../references/configuration.md) for the full
set of environment variables and provider-specific defaults.

## Check there is no VM yet

```shell,script(name="status-before", expected_exit_code=0)
SANDBOX_PROVIDER=hetzner hermes sandbox status
```

```text,verify(script_name="status-before", stream=stdout)
[sandbox] No sandbox VM recorded.
```

## Provision the VM

This creates a real Hetzner server and waits for it to get a public IP.
The id and IP are not predictable, so we just check the exit code — the
next step confirms the VM was recorded.

```shell,script(name="provision", expected_exit_code=0)
SANDBOX_PROVIDER=hetzner hermes sandbox provision
```

## Check the VM status

```shell,script(name="status-after", expected_exit_code=0)
SANDBOX_PROVIDER=hetzner hermes sandbox status | grep '"provider"'
```

```text,verify(script_name="status-after", stream=stdout)
  "provider": "hetzner",
```

## Run commands on the VM

Once the VM is provisioned, Hermes' ssh terminal backend routes every
`terminal`, `file`, and `execute_code` call into it.  Restart Hermes (or
start a new session) and the sandbox is active — your shell commands run
on the VM, not on your host:

```shell,skip()
hermes -z "run: uname -a && docker --version"
```

```shell,skip()
Linux hermes-sandbox 6.8.0-31-generic #31-Ubuntu SMP PREEMPT_DYNAMIC Sat Apr 20 02:24:16 UTC 2024 x86_64 GNU/Linux
Docker version 26.0.0, build 2ae903e
```

## Tear down the VM

When you are done, destroy the server so you stop paying for it:

```shell,script(name="teardown", expected_exit_code=0)
SANDBOX_PROVIDER=hetzner hermes sandbox teardown
```

```text,verify(script_name="teardown", stream=stdout)
[sandbox] VM torn down.
```

## Confirm it is gone

```shell,script(name="status-after-teardown", expected_exit_code=0)
SANDBOX_PROVIDER=hetzner hermes sandbox status
```

```text,verify(script_name="status-after-teardown", stream=stdout)
[sandbox] No sandbox VM recorded.
```
