from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MergeIn(BaseModel):
    survivor_code: str = Field(min_length=2, max_length=48)
    source_codes: list[str] = Field(min_length=1, max_length=20)
    note: str | None = Field(default=None, max_length=500)


class MergePreviewIn(BaseModel):
    survivor_code: str = Field(min_length=2, max_length=48)
    source_codes: list[str] = Field(min_length=1, max_length=20)


class MergeResultOut(BaseModel):
    id: int
    merge_key: str
    survivor_code: str
    source_code: str
    status: Literal["completed"]
    created: bool
    duplicate: bool
