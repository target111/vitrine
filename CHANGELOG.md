# Changelog

Notable changes to vitrine. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-08-01

The internals were substantially rewritten. Nothing exported from `vitrine`
changed; the notes below cover the behaviour that did.

### Added

- `args=True` on `@conv.entry`: the entry command takes the same typed
  arguments `@router.command` does, so a flow can start with what it needs
  already in hand -- `/order ABC 3`, or a `t.me/bot?start=<payload>` deep
  link. Bad arguments get the usage line and the run never starts. States
  and fallbacks keep their parameters injected, as before.
- `bot.sync_commands(chats=...)` syncs just the named chats and touches
  nothing else, not even the default menu. The right call after one
  promotion, and a safe one even while a `scope_chats` resolver is having
  a bad day.

### Changed

- `sync_commands()` skips chats whose menu already holds what it would
  write: a full re-sync after one promotion costs one API round-trip, not
  one per scoped chat.
- Menu publishing is now a `vitrine.commands.CommandMenus` object holding
  the process's record of what it has published, replacing the
  `sync_command_menus()` function and its `published_chats=` argument.
  `vitrine.routing.doc_summary` and `doc_body` are one `split_docstring`
  returning both halves, and `validate_command_name` no longer takes an
  `owner`. Nothing exported from `vitrine` changes.

### Fixed

- A conversation step whose `when=` rejects a button press no longer
  swallows it: `when` narrows the PTB pattern, as it always has outside
  conversations, so a sibling step on the same model gets to match. A press
  every step rejects is answered by a catch-all handler, so the button no
  longer spins until Telegram gives up on it. The catch-all sits last in
  group 0, so keep callback handlers there (the default): Telegram takes one
  answer per press, and an alert from a later group arrives too late to show.
- A conversation entry that starts no run -- refused arguments, a failed
  guard, a bare `Screen` -- no longer ends the caller's other exclusive runs
  or leaves a draft state behind. Peers now end right after the entry's
  screen renders, so a peer's goodbye follows the new flow's hello. An entry
  returning `END` started and finished, so it still counts.
- Naming `"default"` in `Bot(scope_chats=...)` is now a `ConfigurationError`:
  its chats were getting a menu with every default command twice.
- A chat named by more than one scope in `Bot(scope_chats=...)` now gets one
  menu holding all of its scopes' commands. Telegram keeps a single chat
  menu per chat, so writing one scope at a time left whichever came last --
  an admin who was also a vip saw the vip menu and no `/ban`.
- A first `sync_commands()` that fails no longer discards `known_scope_chats`:
  the seed is consumed by the first sync that *completes*, so a database
  down at startup -- exactly when the first sync runs -- no longer orphans
  every historical chat's stale menu for the life of the process.
- Concurrent `sync_commands()` calls are serialized, so overlapping syncs
  can no longer drop each other's bookkeeping and then skip a chat that
  still needed its menu rewritten.

## [0.3.0] - 2026-07-31

### Added

- `/help <command>` shows one command in full: its usage line, the whole
  handler docstring rather than the summary the list has room for, what each
  argument takes and defaults to, and the scope, guards and rate limit that
  govern it. A command the caller cannot see answers exactly as one that does
  not exist, so `/help ban` reveals nothing to a non-admin.

- `edits=True` on `@router.command`, `@router.message`, `@conv.entry` and
  `@conv.state` opts a handler into edited messages, which it no longer sees
  by default.
- Build-time check that an injected parameter's annotation and its provider
  agree: `@bot.provide("count")` returning `str` under `count: int` is now a
  warning on the `vitrine.build` logger at startup instead of an
  `AttributeError` somewhere inside the handler later. Only reported where a
  subclass test settles it -- protocols, generics, unions, unannotated
  factories and signatures with `TYPE_CHECKING`-only names are all left alone,
  since injection is by name and a duck-typed stand-in is legitimate.
  `Bot(strict_types=True)` makes it a `ConfigurationError` instead.
- `Bot.sync_commands()` republishes the Telegram command menus on demand, so
  promoting an admin can update their menu without a restart.
- `Bot(known_scope_chats=...)` names the chats that may still carry a
  chat-scoped menu written by an earlier *run*, which a restart has otherwise
  forgotten. Clearing a chat that has no menu is a no-op, so a generous set --
  every chat a scope has ever resolved to -- is safe. Read once per process, by
  the first sync: only that one has anything to recover, and re-reading would
  re-delete every historical chat's menu on every later `sync_commands()`.

### Changed

- `vitrine.routing.first_doc_line` is now `doc_summary`, since it returns the
  docstring's first paragraph rather than its first line. Nothing exported
  from `vitrine` changes.

### Fixed

- A command description taken from a docstring is no longer cut at the first
  line break, so a summary that wraps across two source lines stays one
  sentence in `/help`, in `/help <command>`, and in the description published
  to Telegram's command menu. The orphaned remainder no longer opens the
  detail text either.
- Editing an old message into a command no longer runs that command, and
  editing any old text message no longer re-feeds it to message handlers,
  reply-keyboard handlers, and whichever conversation state is live *now*.
  Telegram delivers an edit as a fresh update and PTB's default filters match
  those, so `/help` edited into `/whatever` really did run `/whatever`. Pass
  `edits=True` to keep the old behaviour for a given handler.
- Chats that leave a command scope get their chat-scoped menu deleted instead
  of keeping it for good. Telegram stores every scope until something replaces
  it and lets a chat scope shadow the default one, so a demoted admin -- or
  everyone, after one restart with a `scope_chats` resolver that came back
  empty -- kept the menu they were last given and never saw a command added
  since.
- A command name Telegram would reject (uppercase, over 32 characters) is now
  a `ConfigurationError` at registration. PTB's `CommandHandler` lowercases
  before it validates, so `/myCmd` dispatched normally while the same name
  made `setMyCommands` reject the whole batch, freezing every menu in every
  scope at what the previous run published, with only a log line to say so.
- A command whose `scope` has no chats in `Bot(scope_chats=...)` is now a
  `ConfigurationError` from `Bot.build()` rather than a command that reaches
  `/help` and no Telegram menu at all. Only checked when `scope_chats` is
  configured: without it, `scope` groups `/help` and nothing else.
- A handler parameter with an explicit `Depends(...)` default is no longer
  treated as a command argument. Nothing registers a name for it, so it went
  into the usage line and ate a token off the command line that the injector
  then discarded -- and a `Greedy` parameter after it was rejected as
  not-last.

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
[0.4.0]: https://github.com/target111/vitrine/releases/tag/v0.4.0
[0.3.0]: https://github.com/target111/vitrine/releases/tag/v0.3.0
[0.2.0]: https://github.com/target111/vitrine/releases/tag/v0.2.0
[0.1.0]: https://github.com/target111/vitrine/releases/tag/v0.1.0
