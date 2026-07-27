# Changelog

Notable changes to vitrine. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-07-27

### Added

- Conversation entry commands are discoverable: `@conv.entry(command=...)`
  takes `description`, `scope` and `hidden`, and the command now appears in
  `/help` and in the per-scope Telegram command menus like `@router.command`.
- `@conv.state(...)` accepts several state names, the `ANY_STATE` wildcard
  (mount one handler -- typically a Cancel button -- on every state), and
  `command="skip"` for a command that is only valid inside that state.
- `ReplyButton(style=...)`: reply-keyboard buttons take the same styles as
  inline ones (`primary`/`success`/`danger`), validated the same way.
- `Conversation(exclusive=True)`: entering the conversation ends any other
  exclusive run for the same caller (exit hook gets `CANCELLED`, state is
  cleared, the pending timeout job is removed) instead of leaving a
  half-finished flow live to swallow the next answer.

### Changed

- **Breaking:** `@conv.state()` takes its state names as varargs
  (`state(*names)`) so a step can be mounted on several at once. Positional
  calls are unaffected; a keyword call like `conv.state(name="qty")` now
  raises `TypeError` and must drop the keyword.
- Conversation steps and exit hooks are validated at build time like every
  other handler: a parameter nothing can supply is now a `ConfigurationError`
  from `Bot.build()`, not an `InjectionError` on the update that reaches it.
- The default `UsageError` UX returns a `Screen` instead of calling
  `Message.reply_text` itself, so it goes out through `Delivery` like any
  other message. The usage text is now a plain message in the chat rather
  than a reply threaded onto the offending command.

### Fixed

- Command arguments work in modules using `from __future__ import annotations`
  where a handler annotates an injected parameter with a `TYPE_CHECKING`-only
  type (`update: Update`). Evaluating the signature failed on that one name and
  left *every* annotation a string, so `str` arrived as `'str'` and each value
  was rejected with "must be a valid str", while `Greedy` stopped consuming the
  rest of the line. Argument types are now resolved one at a time, after the
  injected parameters are filtered out, so an unresolvable name on one of those
  is irrelevant. An argument whose *own* type cannot be imported at runtime is
  now a `ConfigurationError` from `Bot.build()`, naming the parameter.
  Thanks to @elpekenin for the report and the diagnosis ([#1]).
- A handler whose docstring is only whitespace no longer raises `IndexError`
  at import time when `@router.command` (or a conversation entry) derives its
  description from it; the description is empty instead.

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

[#1]: https://github.com/target111/vitrine/issues/1
[0.2.0]: https://github.com/target111/vitrine/releases/tag/v0.2.0
[0.1.0]: https://github.com/target111/vitrine/releases/tag/v0.1.0
