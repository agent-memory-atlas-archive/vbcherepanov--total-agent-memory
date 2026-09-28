"""Turn AML messages into the fragments TAM stores and Search returns verbatim.

One message becomes one fragment unless it is longer than the configured
limit (text-embedding-v4 accepts at most 8,192 tokens per text); then it is
split on line boundaries. In the `annotated` format each fragment starts with
the message time and role, so the Answer model sees when and by whom it was
said — this is stored metadata, not generated text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from aml_adapter.contracts import Message, content_text

MS_PER_SECOND = 1000


@dataclass(frozen=True)
class Fragment:
    message_index: int
    part_index: int
    role: str
    timestamp_ms: int | None
    text: str


def iso_from_ms(timestamp_ms: int) -> str:
    moment = datetime.fromtimestamp(timestamp_ms / MS_PER_SECOND, tz=UTC)
    if timestamp_ms % MS_PER_SECOND:
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def split_text(text: str, max_chars: int) -> list[str]:
    """Split at line boundaries into pieces of at most `max_chars`; long lines are cut."""
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:max_chars])
            line = line[max_chars:]
        if len(current) + len(line) > max_chars:
            pieces.append(current)
            current = ""
        current += line
    if current:
        pieces.append(current)
    return [piece for piece in pieces if piece.strip()]


def build_fragments(messages: list[Message], *, max_chars: int, content_format: str) -> list[Fragment]:
    fragments: list[Fragment] = []
    for message_index, message in enumerate(messages):
        body = content_text(message.content)
        timestamp_ms = round(message.timestamp) if message.timestamp is not None else None
        if content_format == "annotated":
            stamp = f"[{iso_from_ms(timestamp_ms)}] " if timestamp_ms is not None else ""
            header = f"{stamp}{message.role}: "
        else:
            header = ""
        budget = max(1, max_chars - len(header))
        for part_index, piece in enumerate(split_text(body, budget)):
            fragments.append(Fragment(message_index, part_index, message.role, timestamp_ms, header + piece))
    return fragments
