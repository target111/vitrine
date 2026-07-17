# Changelog

Notable changes to vitrine. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-07-17

First release on PyPI as `vitrine-tg` (import name: `vitrine`).

### Added

- Typed callback data: pydantic models with a prefix, positional and keyed
  wire formats, stale/corrupt payloads answered as "button expired".
- Screens and delivery: message-as-value-object, edit/reply/proactive send,
  text↔media transitions, content-hash `file_id` caching with re-upload retry.
- Dependency injection by parameter name: providers, async generator cleanup,
  build-time validation of handler signatures.
- App-defined principal with guards (`requires`, `admin_only`,
  `requires_principal`) and bot-wide ban enforcement.
- Routers with per-router middleware; raw PTB handlers as an escape hatch.
- Guided conversations with dataclass state, string transitions, timeouts,
  and exit hooks.
- Supervised background workers with exponential-backoff restarts.
- Typed command arguments, auto `/help`, per-scope command menus, pagination,
  rate limiting, a composable Markdown builder, and structured logging.

[0.1.0]: https://github.com/target111/vitrine/releases/tag/v0.1.0
