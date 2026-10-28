# Changelog

Notable changes to vitrine. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.5.0] - Unreleased

Conversations, dependency scopes and handler ordering reworked. Several
changes break existing code; each is listed under **Breaking** with its fix.

### Added

- `vitrine.testing`: `TestClient` drives a bot through PTB's real dispatcher
  with Telegram faked at the HTTP layer (`FakeTelegram`), recording every Bot
  API call. `send`, `press`, `edit` and `feed` each return the calls one
  update produced.
- `Arg[T]` marks a command argument explicitly (type checkers see `T`).
- `CANCEL`: a conversation step returns it to end the run as `CANCELLED`, so
  a Cancel *button* reports the same reason as the `/cancel` fallback.
- An exit hook may return a `Screen`, e.g. a goodbye on `TIMEOUT`.
- `Conversation.active(chat_id=..., user_id=...)` returns a caller's live run.
- `Bot(menu_store=...)` and the `MenuStore` protocol: remember published
  command menus across restarts, so unchanged chats are skipped and chats
  demoted while the bot was down are still cleared.
- `bot.build(request=..., get_updates_request=...)` swaps PTB's HTTP
  transports (pass separate instances: a long poll holds its connection);
  building again with different ones raises. `bot.startup()`, `bot.stop()`
  and `bot.shutdown()` run vitrine's lifecycle for apps that drive the
  application themselves.
- Build-time errors for handlers that could never run: the same command
  registered twice, or a callback handler behind one -- or behind a
  conversation entry -- that takes every press of its model.

### Changed

- **Breaking:** command arguments must be declared with `Arg[T]` (or
  `Greedy`); every other parameter is injected. A parameter nothing can
  supply is now a build error naming it, instead of silently becoming an
  argument, and registering a provider no longer changes a command's syntax.
  Replace `amount: float` with `amount: Arg[float]`.
- **Breaking:** `@conv.entry(args=True)` is gone; an entry command parses the
  `Arg[...]` parameters it declares.
- **Breaking:** `END` is vitrine's own sentinel rather than PTB's
  `ConversationHandler.END`; return `END` (or `CANCEL`) from steps.
- **Breaking:** registering anything after `build()` (or `run()`) raises
  `ConfigurationError` instead of being silently ignored.
- **Breaking:** a caller has at most one live conversation run per chat:
  starting any conversation cancels the caller's run in another one, as
  `exclusive=True` used to. `exclusive`, `per_chat` and `per_user` are gone;
  a run always belongs to one user in one chat. Drop the arguments. The
  cancelled run's exit hook still runs, but a `Screen` it returns is dropped
  so it can't overwrite the new flow's first screen.
- Re-entering a conversation mid-run restarts it (the old run ends as
  `CANCELLED`) instead of being silently ignored.
- A live run's steps always see the caller's updates before any
  conversation's entry points. Before, a conversation registered earlier
  could start on a button press the live run was waiting for.
- **Breaking:** passing both `scope_chats` and `scope_member` is a
  `ConfigurationError`. With `scope_chats`, `/help` shows a chat exactly the
  scopes its command menu carries.
- Conversations run on vitrine's own state machine instead of wrapping PTB's
  `ConversationHandler`: one record per live run, no PTB internals.
  Steps' screens, result
  validation and exit hooks now run inside the pipeline, so they get error
  UX, middleware and the per-update log line.
- One dependency scope per update: the ban gate, every handler group,
  conversation hooks and error handlers share resolved providers and the
  principal, and generator cleanups run after the whole update. A cleanup
  that fails (a commit, say) still reaches `@bot.on_error` handlers.
- The startup command menu sync runs in the background, and Telegram's flood
  control (`RetryAfter`) is waited out instead of skipping the chat.
- Registration decorators are fully typed and return the decorated function
  unchanged.

### Fixed

- A conversation step no longer loses its caller's messages to a plain
  `@bot.message()` handler. Conversations now come before the bot's other
  handlers in group 0; reply-keyboard buttons still come first.
- A principal loaded with a generator provider (a database session) is no
  longer handed to handlers after that provider was cleaned up by the ban
  gate.
- A non-admin in a `scope_chats` scope no longer sees commands in their
  Telegram menu that `/help` then calls unknown.
- A large `scope_chats` no longer delays the bot's first answer until every
  menu is written.

## [0.4.0] - 2026-08-01

Internals rewritten. Nothing exported from `vitrine` changed.

### Added

- `args=True` on `@conv.entry`: an entry command takes typed arguments like
  `@router.command` does, so a flow can start with what it needs (`/order ABC 3`,
  or a `t.me/bot?start=<payload>` deep link). Bad arguments get the usage line
  and the run never starts.
- `bot.sync_commands(chats=...)` syncs only the named chats, leaving every other
  menu untouched, including the default one.

### Changed

- `sync_commands()` skips chats whose menu already holds what it would write, so
  a full re-sync costs one API round-trip rather than one per scoped chat.
- `commands.sync_command_menus()` is now a `CommandMenus` object owning the
  process's record of what it published. `routing.doc_summary` and `doc_body`
  are one `split_docstring`; `validate_command_name` drops its `owner` argument.
  None of these are exported.

### Fixed

- A conversation step whose `when=` rejects a press no longer swallows it, so a
  sibling step on the same model can match. A press no step takes is answered by
  a catch-all, which sits last in group 0; keep callback handlers there, since
  Telegram accepts one answer per press.
- A conversation entry that starts no run (refused arguments, a failed guard, a
  bare `Screen`) no longer ends the caller's other exclusive runs or leaves draft
  state behind. Peers now end after the entry's screen renders. An entry
  returning `END` still counts as having run.
- Naming `"default"` in `Bot(scope_chats=...)` is a `ConfigurationError`. Those
  chats were getting every default command listed twice.
- A chat named by several scopes now gets one menu holding all of their commands.
  Telegram stores a single chat-scoped menu, so writing one scope at a time left
  whichever wrote last.
- A failed first `sync_commands()` no longer discards `known_scope_chats`. The
  seed is consumed by the first sync that completes, so a database that is down
  at startup no longer orphans every stale menu for the life of the process.
- Concurrent `sync_commands()` calls are serialized, including the scope read.

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
[0.5.0]: https://github.com/target111/vitrine/releases/tag/v0.5.0
[0.4.0]: https://github.com/target111/vitrine/releases/tag/v0.4.0
[0.3.0]: https://github.com/target111/vitrine/releases/tag/v0.3.0
[0.2.0]: https://github.com/target111/vitrine/releases/tag/v0.2.0
[0.1.0]: https://github.com/target111/vitrine/releases/tag/v0.1.0
