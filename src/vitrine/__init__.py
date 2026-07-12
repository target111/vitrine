"""vitrine — a batteries-included foundation on top of python-telegram-bot.

Thin in ceremony, deep in capability: typed callback routing, screens with
robust delivery, dependency injection, an app-defined principal, guided
conversations, supervised workers, rate limiting, structured logging, and
typed error UX — while PTB's dispatcher, handlers, and filters stay fully
reachable underneath.
"""

from .app import Bot, VitrineContext
from .args import Greedy
from .auth import Auth, admin_only, requires, requires_principal
from .callbacks import CallbackData
from .conversations import END, Conversation, ExitReason
from .exceptions import (
    AuthError,
    BannedError,
    CallbackDataError,
    ConfigurationError,
    FrameworkError,
    InjectionError,
    NotAuthorizedError,
    NotRegisteredError,
    RateLimitedError,
    UsageError,
    UserFacingError,
)
from .injection import Depends, Invocation, Providers
from .logging import audit, log_event, setup_logging
from .media import FileIdCache, InMemoryFileIdCache, download
from .middleware import Event
from .pagination import ListSource, Page, PageSource, Paginator, nav_row
from .ratelimit import throttle
from .routing import Router
from .screens import (
    Animation,
    Audio,
    Button,
    ButtonStyle,
    Delivery,
    Document,
    Media,
    Photo,
    Screen,
    Video,
    Voice,
)

__all__ = [
    "Animation",
    "Audio",
    "Auth",
    "AuthError",
    "BannedError",
    "Bot",
    "Button",
    "ButtonStyle",
    "CallbackData",
    "CallbackDataError",
    "ConfigurationError",
    "Conversation",
    "Delivery",
    "Depends",
    "Document",
    "END",
    "Event",
    "ExitReason",
    "VitrineContext",
    "FileIdCache",
    "FrameworkError",
    "Greedy",
    "InMemoryFileIdCache",
    "InjectionError",
    "Invocation",
    "ListSource",
    "Media",
    "NotAuthorizedError",
    "NotRegisteredError",
    "Page",
    "PageSource",
    "Paginator",
    "Photo",
    "Providers",
    "RateLimitedError",
    "Router",
    "Screen",
    "UsageError",
    "UserFacingError",
    "Video",
    "Voice",
    "admin_only",
    "audit",
    "download",
    "log_event",
    "nav_row",
    "requires",
    "requires_principal",
    "setup_logging",
    "throttle",
]
