"""Synchronous, SDK-independent boundary for explicitly approved review requests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ReviewImage:
    image_id: str
    data: bytes


@dataclass(frozen=True)
class ReviewRequest:
    model: str
    prompt: str
    images: tuple[ReviewImage, ...]
    batch_size: int
    max_image_bytes: int


@dataclass(frozen=True)
class ReviewReply:
    text: str
    metadata: dict


class ReviewProvider(Protocol):
    """One instance can receive concurrent review calls; requests share no session."""

    def review(self, request: ReviewRequest) -> ReviewReply: ...

    def models(self) -> list[dict]: ...
