"""Keyset (cursor) pagination.

Offset pagination drifts: rows inserted while a client pages cause skips and
duplicates. Keyset pagination anchors on the last row seen, so results stay
stable under concurrent writes — which matters here because sessions, runs, and
documents are created continuously by agents, not just by humans.

Cursors are opaque base64 of ``created_at|id``. The id breaks ties, so ordering
is total even when timestamps collide.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, and_, or_

from hoursx.errors import ValidationError

T = TypeVar("T")


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    id: str

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Cursor:
        padding = "=" * (-len(token) % 4)
        try:
            raw = base64.urlsafe_b64decode(token + padding).decode()
            timestamp, _, identifier = raw.partition("|")
            if not identifier:
                raise ValueError("cursor missing id component")
            return cls(created_at=datetime.fromisoformat(timestamp), id=identifier)
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise ValidationError(f"malformed cursor: {token!r}") from exc


@dataclass
class Page(Generic[T]):
    """One page plus the cursor to fetch the next."""

    items: list[T]
    next_cursor: str | None

    def as_dict(self, serialize: Any) -> dict[str, Any]:
        return {
            "items": [serialize(item) for item in self.items],
            "next_cursor": self.next_cursor,
        }


def resolve_page_size(requested: int | None, *, default: int, maximum: int) -> int:
    """Clamp a client-requested page size into the configured bounds."""
    if requested is None:
        return default
    if requested < 1:
        raise ValidationError("limit must be at least 1")
    return min(requested, maximum)


def apply_keyset(
    statement: Select,
    *,
    created_at_column: Any,
    id_column: Any,
    cursor: str | None,
    limit: int,
) -> Select:
    """Order newest-first and seek past *cursor*.

    Fetches ``limit + 1`` rows; the caller uses the extra row to decide whether
    a next page exists without a second count query.
    """
    statement = statement.order_by(created_at_column.desc(), id_column.desc())
    if cursor:
        anchor = Cursor.decode(cursor)
        statement = statement.where(
            or_(
                created_at_column < anchor.created_at,
                and_(created_at_column == anchor.created_at, id_column < anchor.id),
            )
        )
    return statement.limit(limit + 1)


def build_page(rows: list[T], *, limit: int) -> Page[T]:
    """Split the over-fetched row list into a page plus its next cursor."""
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = Cursor(created_at=last.created_at, id=last.id).encode()  # type: ignore[attr-defined]
    return Page(items=items, next_cursor=next_cursor)
