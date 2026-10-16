DRAFT_PLANNED = "draft_planned"
CONTEXT_READY = "context_ready"
WRITING = "writing"
REVIEW_PENDING = "review_pending"
REVISION_PENDING = "revision_pending"
APPROVED = "approved"
HUMAN_REVIEW_OPTIONAL = "human_review_optional"
PERSISTED = "persisted"
PUBLISH_PENDING = "publish_pending"
DRAFT_UPLOADED = "draft_uploaded"
PUBLISH_FAILED = "publish_failed"
ARCHIVED = "archived"

UNKNOWN_STATE_ERROR = "unknown state"
INVALID_TRANSITION_ERROR = "invalid transition"

ALLOWED_TRANSITIONS = {
    DRAFT_PLANNED: {CONTEXT_READY},
    CONTEXT_READY: {WRITING},
    WRITING: {REVIEW_PENDING},
    REVIEW_PENDING: {REVISION_PENDING, APPROVED},
    REVISION_PENDING: {REVIEW_PENDING, APPROVED},
    APPROVED: {HUMAN_REVIEW_OPTIONAL, PERSISTED},
    HUMAN_REVIEW_OPTIONAL: {PERSISTED},
    PERSISTED: {PUBLISH_PENDING},
    PUBLISH_PENDING: {DRAFT_UPLOADED, PUBLISH_FAILED},
    DRAFT_UPLOADED: {ARCHIVED},
    PUBLISH_FAILED: {PUBLISH_PENDING, ARCHIVED},
}


class ChapterStateMachine:
    def __init__(self) -> None:
        self.allowed_transitions = {
            state: set(targets) for state, targets in ALLOWED_TRANSITIONS.items()
        }

    def transition(self, current: str, target: str) -> str:
        if current not in self.allowed_transitions:
            raise ValueError(f"{UNKNOWN_STATE_ERROR}: {current}")
        if target not in self.allowed_transitions[current]:
            raise ValueError(f"{INVALID_TRANSITION_ERROR}: {current} -> {target}")
        return target
