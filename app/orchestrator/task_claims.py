from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, or_, update

LEASE_SECONDS = 1800


def utc_now():
    return datetime.now(timezone.utc)


def ready_task(model, status, now):
    return and_(
        model.status == status,
        or_(model.next_attempt_at.is_(None), model.next_attempt_at <= now),
        or_(model.claim_token.is_(None), model.lease_expires_at <= now),
    )


def claim_task(session, model, item_id, status, *, running_status=None):
    now = utc_now()
    token = uuid4().hex
    values = {
        "claim_token": token,
        "lease_expires_at": now + timedelta(seconds=LEASE_SECONDS),
        "attempt_count": model.attempt_count + 1,
        "next_attempt_at": None,
        "error_message": None,
    }
    if running_status:
        values["status"] = running_status
    result = session.execute(update(model).where(
        model.id == item_id, ready_task(model, status, now)
    ).values(**values).execution_options(synchronize_session=False))
    session.commit()
    return token if result.rowcount == 1 else None
