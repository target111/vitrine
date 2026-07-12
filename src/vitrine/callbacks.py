"""Type-safe callback data.

Define callback payloads as pydantic models with a short prefix::

    class MenuCB(CallbackData, prefix="menu"):
        section: str
        page: int = 1

``MenuCB(section="shop").pack()`` produces the ``callback_data`` string;
routing decodes and validates it back into a ``MenuCB`` instance which is
injected into the handler as ``data``. Malformed or stale payloads raise
:class:`CallbackDataError`, which the dispatch layer turns into a harmless
"button expired" answer instead of a crash.

Two wire formats:

- **positional** (default): ``menu:shop:2`` — compact, but field order is part
  of the contract;
- **keyed** (``keyed=True``): ``menu?section=shop&page=2`` — self-describing.
  Fields that equal their default are omitted, unknown keys are ignored, so
  payloads survive fields being added, reordered, or removed — buttons from
  before a schema change keep working.

::

    class MenuCB(CallbackData, prefix="menu", keyed=True):
        section: str
        page: int = 1

``keyed`` only selects what :meth:`~CallbackData.pack` writes;
:meth:`~CallbackData.unpack` detects the format from the data itself, so a
model can be switched to ``keyed=True`` while old positional buttons are still
live.
"""

from __future__ import annotations

import urllib.parse
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from .exceptions import CallbackDataError

SEP = ":"
KEYED_SEP = "?"
PAIR_SEP = "&"
KV_SEP = "="
MAX_LEN = 64  # Telegram's callback_data limit in bytes

#: characters that would make a prefix ambiguous on the wire
_FORBIDDEN_PREFIX_CHARS = SEP + KEYED_SEP + PAIR_SEP + KV_SEP

_registry: dict[str, type["CallbackData"]] = {}


def _encode_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool):
        value = int(value)

    return urllib.parse.quote(str(value), safe="")


def _split_prefix(data: str) -> tuple[str, str, str]:
    """Split raw data into (prefix, separator, rest); separator is '', ':' or '?'."""
    for index, char in enumerate(data):
        if char == SEP or char == KEYED_SEP:
            return data[:index], char, data[index + 1 :]

    return data, "", ""


class CallbackData(BaseModel):
    """Base class for typed callback payloads. Subclass with ``prefix="..."``."""

    __prefix__: ClassVar[str]
    __keyed__: ClassVar[bool] = False

    def __init_subclass__(
        cls, prefix: str | None = None, keyed: bool | None = None, **kwargs: Any
    ) -> None:
        super().__init_subclass__(**kwargs)

        if keyed is not None:
            cls.__keyed__ = keyed

        if prefix is None:
            return  # abstract intermediate subclass

        if any(char in prefix for char in _FORBIDDEN_PREFIX_CHARS):
            raise CallbackDataError(
                f"prefix {prefix!r} must not contain any of {_FORBIDDEN_PREFIX_CHARS!r}"
            )

        existing = _registry.get(prefix)
        if existing is not None and existing is not cls:
            raise CallbackDataError(
                f"callback prefix {prefix!r} already used by {existing.__name__}"
            )

        cls.__prefix__ = prefix
        _registry[prefix] = cls

    # -- encoding ---------------------------------------------------------------

    def pack(self) -> str:
        """Encode to a compact ``callback_data`` string (<= 64 bytes)."""
        packed = self._pack_keyed() if self.__keyed__ else self._pack_positional()

        if len(packed.encode()) > MAX_LEN:
            raise CallbackDataError(
                f"packed callback data for {type(self).__name__} is "
                f"{len(packed.encode())} bytes (Telegram limit is {MAX_LEN}): {packed!r}"
            )

        return packed

    def _pack_positional(self) -> str:
        parts = [self.__prefix__]
        parts += [_encode_value(getattr(self, name)) for name in type(self).model_fields]
        return SEP.join(parts)

    def _pack_keyed(self) -> str:
        # defaults are dropped (that's what keeps payloads stable); pydantic
        # already knows which fields still equal their default.
        pairs = [
            f"{name}{KV_SEP}{_encode_value(value)}"
            for name, value in self.model_dump(exclude_defaults=True).items()
        ]

        if not pairs:
            return self.__prefix__

        return f"{self.__prefix__}{KEYED_SEP}{PAIR_SEP.join(pairs)}"

    # -- decoding ---------------------------------------------------------------

    @classmethod
    def unpack(cls, data: str) -> "CallbackData":
        """Decode and validate; raises :class:`CallbackDataError` on any problem.

        The wire format (positional vs keyed) is detected from the data, so
        both decode regardless of the model's ``keyed`` setting.
        """
        prefix, sep, rest = _split_prefix(data)
        if prefix != cls.__prefix__:
            raise CallbackDataError(f"data {data!r} does not match prefix {cls.__prefix__!r}")

        if sep == KEYED_SEP:
            values = cls._parse_keyed(data, rest)
        else:
            values = cls._parse_positional(data, rest)

        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise CallbackDataError(f"invalid callback data {data!r}: {exc}") from exc

    @classmethod
    def _parse_positional(cls, data: str, rest: str) -> dict[str, Any]:
        field_names = list(cls.model_fields)
        raw_parts = rest.split(SEP) if rest else []
        if len(raw_parts) > len(field_names):
            raise CallbackDataError(f"too many values in {data!r} for {cls.__name__}")

        values: dict[str, Any] = {}
        for name, part in zip(field_names, raw_parts):
            if part == "":
                continue  # empty -> omitted; pydantic applies the default / None
            values[name] = urllib.parse.unquote(part)

        return values

    @classmethod
    def _parse_keyed(cls, data: str, rest: str) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for pair in rest.split(PAIR_SEP) if rest else []:
            key, eq, raw = pair.partition(KV_SEP)
            if not eq:
                raise CallbackDataError(f"malformed pair {pair!r} in {data!r}")
            if key not in cls.model_fields:
                continue  # unknown key: tolerated so old buttons survive schema changes
            values[key] = None if raw == "" else urllib.parse.unquote(raw)

        return values

    @classmethod
    def matches(cls, data: object) -> bool:
        """PTB ``pattern`` predicate: does this raw string belong to this model?"""
        if not isinstance(data, str):
            return False

        prefix, _, _ = _split_prefix(data)
        return prefix == cls.__prefix__


def decode(data: str) -> CallbackData:
    """Decode using the global prefix registry (any registered model)."""
    prefix, _, _ = _split_prefix(data)
    cls = _registry.get(prefix)
    if cls is None:
        raise CallbackDataError(f"no callback model registered for prefix {prefix!r}")

    return cls.unpack(data)
