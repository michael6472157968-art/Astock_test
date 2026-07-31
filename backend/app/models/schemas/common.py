"""统一API响应模型——所有接口强制使用此格式。

{
  "code": 200,
  "message": "success",
  "data": {},
  "timestamp": 1751300000,
  "ext_info": {}
}
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    code: int = Field(default=200)
    message: str = Field(default="success")
    data: T | None = None
    timestamp: int = Field(default=0)
    ext_info: dict[str, Any] = Field(default_factory=dict)


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class PaginatedData(BaseModel, Generic[T]):
    total: int
    page: int
    page_size: int
    items: list[T]
