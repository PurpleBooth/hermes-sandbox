# Planning Documents

Historical design and implementation plans for the sandbox plugin.  All
features described here have been **implemented and shipped** — these
documents are kept for architectural context and design rationale, not as
pending work.

For user-facing documentation, see the [CLI Reference](../references/cli.md),
[Configuration Reference](../references/configuration.md), and the
[task guides](../tasks/).

## Plans

| Date | Plan | What it delivered |
|---|---|---|
| 2026-06-21 | [DDD / Quality / Functional Refactor](2026-06-21-ddd-quality-refactor.md) | Domain-driven layering, value objects (`SandboxConfig`, `SandboxStatus`), encapsulation fixes, narrowed exception handling, DigitalOcean bug fix, DRY poll loops. |
| 2026-06-21 | [Round 2: Properties, Typed Errors, DRY Providers](2026-06-21-round2-properties-typed-errors.md) | Property-based testing with Hypothesis, typed `ApiError` exception replacing string matching, shared base-class extraction for cloud providers. |
| 2026-06-21 | [Multi-Session, List, and Recreate](2026-06-21-multi-session-list-recreate.md) | `SANDBOX_MODE` (shared / isolated), per-session VM isolation, `hermes sandbox list`, and `hermes sandbox recreate` subcommands. |
| 2026-06-22 | [QEMU Local Provider](2026-06-22-qemu-provider.md) | Local `qemu` provider with cloud-init seed disk, host-forwarded SSH port, automatic Ubuntu 24.04 image download and caching. |
