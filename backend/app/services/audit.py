"""Audit trail helper (D14).

record_audit adds the audit row to the CALLER'S transaction and NEVER commits.
The caller changes state and calls record_audit in the same session, then commits once.
If record_audit raises, the caller rolls back, and the state change disappears with it.
"""
import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent


def _snapshot(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def record_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: Any,
    before: Any = None,
    after: Any = None,
    correlation_id: str | None = None,
) -> AuditEvent:
    for name, value in (("actor", actor), ("action", action), ("entity_type", entity_type)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
    if entity_id is None or not str(entity_id).strip():
        raise ValueError("entity_id is required")

    event = AuditEvent(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before_state=_snapshot(before),
        after_state=_snapshot(after),
        correlation_id=correlation_id,
    )
    session.add(event)
    session.flush()  # surface a database error NOW, inside the caller's transaction
    return event