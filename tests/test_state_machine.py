import pytest

from app.orchestrator.retry_policy import (
    PLATFORM_ERROR,
    TRANSIENT_ERROR,
    classify_error,
    max_retries,
)
from app.orchestrator.state_machine import (
    APPROVED,
    DRAFT_PLANNED,
    INVALID_TRANSITION_ERROR,
    PUBLISH_PENDING,
    REVIEW_PENDING,
    UNKNOWN_STATE_ERROR,
    ChapterStateMachine,
)


def test_reject_publish_before_approval():
    machine = ChapterStateMachine()

    with pytest.raises(ValueError, match=INVALID_TRANSITION_ERROR):
        machine.transition(DRAFT_PLANNED, PUBLISH_PENDING)


def test_allow_review_pending_to_approved():
    machine = ChapterStateMachine()

    assert machine.transition(REVIEW_PENDING, APPROVED) == APPROVED


def test_reject_unknown_current_state():
    machine = ChapterStateMachine()

    with pytest.raises(ValueError, match=UNKNOWN_STATE_ERROR):
        machine.transition("not_a_real_state", DRAFT_PLANNED)


def test_retry_policy_for_timeout_error():
    assert classify_error("timeout_api") == TRANSIENT_ERROR
    assert max_retries(TRANSIENT_ERROR) == 2


def test_retry_policy_for_platform_error():
    assert classify_error("platform_rate_limit") == PLATFORM_ERROR


def test_reject_unknown_error_type_for_max_retries():
    with pytest.raises(ValueError, match="unknown error type"):
        max_retries("unexpected_error_type")
