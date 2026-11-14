"""Shared handling of the optional Idempotency-Key and X-Correlation-ID request headers."""
import re
from typing import Annotated

from fastapi import Header, HTTPException

from app.core.rules import HEADER_TOKEN_PATTERN

IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
CorrelationHeader = Annotated[str | None, Header(alias="X-Correlation-ID")]

_TOKEN = re.compile(HEADER_TOKEN_PATTERN)


def clean_header(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not _TOKEN.match(value):
        raise HTTPException(422, f"{name} must be 1-64 characters from A-Z a-z 0-9 . _ : -")
    return value