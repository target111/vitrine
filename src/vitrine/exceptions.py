"""Framework exception hierarchy.

Errors that end users should see subclass :class:`UserFacingError`; the error
layer renders their ``message`` as friendly UX (callback answer, reply, or a
screen) instead of a stack trace.
"""

from __future__ import annotations

from collections.abc import Sequence


class FrameworkError(Exception):
    """Base class for all vitrine errors."""


class ConfigurationError(FrameworkError):
    """The app wired something up wrong; raised at build time when possible."""


class InjectionError(ConfigurationError):
    """A handler parameter could not be resolved to any known value."""


class CallbackDataError(FrameworkError):
    """Callback data failed to encode/decode (too long, bad prefix, bad values)."""


class UserFacingError(FrameworkError):
    """An error whose message is safe and intended to be shown to the user."""

    def __init__(self, message: str, *, show_alert: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.show_alert = show_alert


class UsageError(UserFacingError):
    """Command arguments did not match the declared signature."""

    def __init__(self, usage: str, hint: str | None = None) -> None:
        text = usage if hint is None else f"{hint}\n{usage}"
        super().__init__(text)
        self.usage = usage
        self.hint = hint


class AuthError(UserFacingError):
    """Base class for identity/permission failures."""


class NotAuthorizedError(AuthError):
    """The resolved principal lacks a required role."""

    def __init__(
        self,
        message: str = "You are not allowed to do that.",
        *,
        missing_roles: Sequence[str] = (),
    ) -> None:
        super().__init__(message, show_alert=True)
        self.missing_roles = tuple(missing_roles)


class BannedError(AuthError):
    """The resolved principal is banned bot-wide."""

    def __init__(self, message: str = "You are banned from using this bot.") -> None:
        super().__init__(message, show_alert=True)


class RateLimitedError(UserFacingError):
    """The caller hit a throttle; ``retry_after`` is in seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Slow down — try again in {max(1, round(retry_after))}s.")
        self.retry_after = retry_after
