"""File and image handling: downloads with cleanup, and content-hash file_id caching.

The :class:`FileIdCache` is shared with the Screen render path, so a media file
is uploaded to Telegram once and re-sent by ``file_id`` on every later render.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol, runtime_checkable

from telegram import Bot


@runtime_checkable
class FileIdCache(Protocol):
    """Maps a stable content key -> Telegram ``file_id``. Plug in Redis/DB freely."""

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, file_id: str) -> None: ...

    async def discard(self, key: str) -> None: ...


class InMemoryFileIdCache:
    """Default cache; lives for the process lifetime."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, file_id: str) -> None:
        self._data[key] = file_id

    async def discard(self, key: str) -> None:
        self._data.pop(key, None)


def content_key(source: bytes | str | Path) -> str:
    """Stable cache key for a media source: content hash for bytes/paths, the URL itself for URLs."""
    if isinstance(source, bytes):
        return "sha256:" + hashlib.sha256(source).hexdigest()

    if isinstance(source, Path):
        return "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()

    return "url:" + source


@asynccontextmanager
async def download(
    bot: Bot,
    file_id: str,
    *,
    timeout: float = 60.0,
    directory: str | Path | None = None,
    suffix: str = "",
) -> AsyncIterator[Path]:
    """Download a Telegram file to a temp path; the file is deleted on exit.

    Usage::

        async with download(bot, message.document.file_id) as path:
            process(path)
    """
    fd, name = tempfile.mkstemp(suffix=suffix, dir=str(directory) if directory else None)
    os.close(fd)
    path = Path(name)

    try:
        async with asyncio.timeout(timeout):
            tg_file = await bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=path)
        yield path
    finally:
        path.unlink(missing_ok=True)
