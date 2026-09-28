"""Typed AML cycle-2 Add/Search contract (https://agentmemoryleaderboard.ai/api-guide)."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from aml_adapter.errors import ContractError

NonEmpty = Annotated[StrictStr, Field(min_length=1)]


class _DTO(BaseModel):
    # Unknown fields are ignored so a contract addition on the platform side
    # does not turn every request into a 422.
    model_config = ConfigDict(extra="ignore", frozen=True)


class TextPart(_DTO):
    type: Literal["text"]
    text: StrictStr


class ImageUrl(_DTO):
    url: StrictStr


class ImagePart(_DTO):
    type: Literal["image_url"]
    image_url: ImageUrl


ContentPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]
Content = StrictStr | list[ContentPart]


class Message(_DTO):
    role: NonEmpty
    timestamp: Annotated[float, Field(ge=0)] | None = None  # Unix milliseconds
    content: Content


class AddRequest(_DTO):
    request_id: NonEmpty
    messages: Annotated[list[Message], Field(min_length=1)]
    user_id: NonEmpty
    session_id: NonEmpty

    def fingerprint(self) -> str:
        """Hash of everything a retry must repeat verbatim besides the ids."""
        body = self.model_dump(mode="json", include={"session_id", "messages"})
        blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AddResponse(_DTO):
    success: bool
    request_id: str
    user_id: str
    session_id: str


class SearchRequest(_DTO):
    query: Content
    options: list[StrictStr] | None = None
    user_id: NonEmpty
    top_k: Annotated[StrictInt, Field(ge=1)]


class SearchHit(_DTO):
    id: str
    content: str
    score: float | None = None
    created_at: str | None = None


class SearchResponse(_DTO):
    data: list[SearchHit]


def content_text(content: str | list[TextPart | ImagePart]) -> str:
    """Text of a message/query; image parts are refused (text tracks only)."""
    if isinstance(content, str):
        text = content
    else:
        pieces = []
        for part in content:
            if isinstance(part, ImagePart):
                raise ContractError("image_url content is not supported by this deployment (text tracks only)")
            pieces.append(part.text)
        text = "\n".join(pieces)
    if not text.strip():
        raise ContractError("content must contain non-empty text")
    return text
