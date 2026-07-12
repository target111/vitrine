"""Composable Markdown builder with safe escaping for Markdown (V1) and MarkdownV2.

Content is a tree of nodes, so styles nest naturally::

    from vitrine.markdown import Md, bold, link

    text = (
        Md()
        .line(bold("Order ", link("#1042", "https://shop.example/o/1042")))
        .bullet("amount: ", code("0.5 BTC"))
        .line(italic("thanks for shopping with us"))
    )
    text.render(2)   # MarkdownV2 string, user input safely escaped

Plain strings are always escaped; use :func:`raw` as the escape hatch for
pre-rendered fragments.
"""

from __future__ import annotations

from typing import Union

_V2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!")
_V1_SPECIAL = set("_*`[")

Inline = Union[str, "Node"]


def escape(text: str, version: int = 2) -> str:
    """Escape ``text`` so it cannot break markup in the given Markdown version."""
    special = _V2_SPECIAL if version == 2 else _V1_SPECIAL
    return "".join(f"\\{ch}" if ch in special else ch for ch in text)


class Node:
    """Base class for all markdown fragments."""

    def render(self, version: int = 2) -> str:
        raise NotImplementedError

    def __add__(self, other: Inline) -> "Seq":
        return Seq(self, other)

    def __radd__(self, other: Inline) -> "Seq":
        return Seq(other, self)


def _node(value: Inline) -> Node:
    if isinstance(value, Node):
        return value
    return Text(str(value))


class Text(Node):
    """Literal text; escaped on render."""

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self, version: int = 2) -> str:
        return escape(self.text, version)


class Raw(Node):
    """Pre-rendered markdown passed through verbatim. Use with care."""

    def __init__(self, markup: str) -> None:
        self.markup = markup

    def render(self, version: int = 2) -> str:
        return self.markup


class Seq(Node):
    """A sequence of fragments rendered back to back."""

    def __init__(self, *children: Inline) -> None:
        self.children = [_node(c) for c in children]

    def render(self, version: int = 2) -> str:
        return "".join(c.render(version) for c in self.children)


class Style(Node):
    """Wraps children in style markers; nests freely."""

    #: name -> (v1 markers, v2 markers); None means unsupported in that version
    _MARKERS: dict[str, tuple[tuple[str, str] | None, tuple[str, str]]] = {
        "bold": (("*", "*"), ("*", "*")),
        "italic": (("_", "_"), ("_", "_")),
        "underline": (None, ("__", "__")),
        "strikethrough": (None, ("~", "~")),
        "spoiler": (None, ("||", "||")),
    }

    def __init__(self, style: str, *children: Inline) -> None:
        self.style = style
        self.body = Seq(*children)

    def render(self, version: int = 2) -> str:
        v1, v2 = self._MARKERS[self.style]
        markers = v2 if version == 2 else v1
        inner = self.body.render(version)
        if markers is None:  # graceful V1 fallback: plain text
            return inner

        open_, close = markers
        return f"{open_}{inner}{close}"


class Code(Node):
    """Inline monospace. Contents are code-escaped, not markdown-escaped."""

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self, version: int = 2) -> str:
        if version == 2:
            body = self.text.replace("\\", "\\\\").replace("`", "\\`")
        else:
            body = self.text.replace("`", "'")

        return f"`{body}`"


class Pre(Node):
    """A code block, optionally with a language tag."""

    def __init__(self, text: str, language: str | None = None) -> None:
        self.text = text
        self.language = language

    def render(self, version: int = 2) -> str:
        if version == 2:
            body = self.text.replace("\\", "\\\\").replace("`", "\\`")
        else:
            body = self.text

        lang = self.language or ""
        return f"```{lang}\n{body}\n```"


class Link(Node):
    """An inline link; the label may be any fragment (e.g. bold text)."""

    def __init__(self, label: Inline, url: str) -> None:
        self.label = _node(label)
        self.url = url

    def render(self, version: int = 2) -> str:
        url = self.url.replace("\\", "\\\\").replace(")", "\\)")
        return f"[{self.label.render(version)}]({url})"


def text(value: object) -> Text:
    return Text(str(value))


def raw(markup: str) -> Raw:
    return Raw(markup)


def bold(*children: Inline) -> Style:
    return Style("bold", *children)


def italic(*children: Inline) -> Style:
    return Style("italic", *children)


def underline(*children: Inline) -> Style:
    return Style("underline", *children)


def strikethrough(*children: Inline) -> Style:
    return Style("strikethrough", *children)


def spoiler(*children: Inline) -> Style:
    return Style("spoiler", *children)


def code(value: object) -> Code:
    return Code(str(value))


def pre(value: str, language: str | None = None) -> Pre:
    return Pre(value, language)


def link(label: Inline, url: str) -> Link:
    return Link(label, url)


def mention(label: Inline, user_id: int) -> Link:
    return Link(label, f"tg://user?id={user_id}")


class Md(Node):
    """Fluent line-oriented document builder.

    Every method returns ``self`` so calls chain; every argument may be a plain
    string (escaped) or any :class:`Node`.
    """

    def __init__(self, *parts: Inline) -> None:
        self._lines: list[Seq] = []
        if parts:
            self.line(*parts)

    def line(self, *parts: Inline) -> "Md":
        self._lines.append(Seq(*parts))
        return self

    def blank(self) -> "Md":
        self._lines.append(Seq())
        return self

    def heading(self, *parts: Inline) -> "Md":
        return self.line(bold(*parts))

    def bullet(self, *parts: Inline) -> "Md":
        return self.line(raw("• "), *parts)

    def kv(self, key: Inline, value: Inline) -> "Md":
        return self.line(bold(key), raw(": "), value)

    def render(self, version: int = 2) -> str:
        return "\n".join(line.render(version) for line in self._lines)
