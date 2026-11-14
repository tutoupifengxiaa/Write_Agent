from __future__ import annotations

from datetime import timedelta
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select, update

from app.agents.context_builder import build_chapter_context_from_memory
from app.agents.persister import persist_approved_chapter, persist_review_candidate
from app.db.models import Chapter
from app.orchestrator.retry_policy import classify_error, max_retries
from app.orchestrator.state_machine import (
    APPROVED,
    CONTEXT_READY,
    PERSISTED,
    REVIEW_PENDING,
    REVISION_PENDING,
    WRITING,
    ChapterStateMachine,
)
from app.orchestrator.task_claims import LEASE_SECONDS, claim_task, utc_now
from app.services.llm_client import LLMConfigurationError


class ChapterWorkflowState(TypedDict, total=False):
    chapter_id: str
    workflow_status: str
    context: dict
    draft: dict
    content: str
    initial_review: dict
    revision: dict | None
    final_review: dict
    mapping_complete: bool
    unresolved_issue_ids: list[str]
    final_status: str
    item_type: str
    item_id: str
    claim_token: str
    base_version_id: str | None
    base_title: str
    base_goal: dict


class StaleGenerationError(Exception):
    pass


class ChapterWorkflow:
    def __init__(
        self,
        *,
        session,
        writer,
        reviewer,
        reviser=None,
        embedding_client=None,
        embedding_model_name: str = "keyword-hash",
        fallback_embedding_client=None,
        embedding_fallback_mode: str = "keyword",
        create_publish_job: bool = False,
    ) -> None:
        self.session = session
        self.writer = writer
        self.reviewer = reviewer
        self.reviser = reviser
        self.embedding_client = embedding_client
        self.embedding_model_name = embedding_model_name
        self.fallback_embedding_client = fallback_embedding_client
        self.embedding_fallback_mode = embedding_fallback_mode
        self.create_publish_job = create_publish_job
        self.state_machine = ChapterStateMachine()
        self.graph = self._compile_graph()

    def invoke(self, chapter_id: str) -> dict:
        chapter = self._get_chapter(chapter_id)
        self.state_machine.transition(chapter.status, CONTEXT_READY)
        token = claim_task(self.session, Chapter, chapter_id, "draft_planned")
        if token is None:
            return {"final_status": "skipped", "item_type": "chapter", "item_id": chapter_id}
        try:
            result = self.graph.invoke({"chapter_id": chapter_id, "claim_token": token})
        except LLMConfigurationError:
            self.session.rollback()
            self.session.execute(update(Chapter).where(
                Chapter.id == chapter_id,
                Chapter.claim_token == token,
                Chapter.status == "draft_planned",
            ).values(
                claim_token=None, lease_expires_at=None,
                attempt_count=Chapter.attempt_count - 1,
            ))
            self.session.commit()
            raise
        except StaleGenerationError:
            self.session.rollback()
            self.session.execute(update(Chapter).where(
                Chapter.id == chapter_id, Chapter.claim_token == token,
            ).values(claim_token=None, lease_expires_at=None))
            self.session.commit()
            return {"final_status": "superseded", "item_type": "chapter", "item_id": chapter_id}
        except Exception as exc:
            self.session.rollback()
            chapter = self._get_chapter(chapter_id)
            error_type = classify_error(exc)
            retry = chapter.attempt_count <= max_retries(error_type)
            self.session.execute(update(Chapter).where(
                Chapter.id == chapter_id, Chapter.claim_token == token,
                Chapter.status == "draft_planned",
            ).values(
                status="draft_planned" if retry else "generation_failed",
                next_attempt_at=utc_now() + timedelta(seconds=5 * 2 ** min(chapter.attempt_count - 1, 8)) if retry else None,
                error_message=f"{type(exc).__name__}: {error_type}",
                claim_token=None, lease_expires_at=None,
            ))
            self.session.commit()
            raise
        return {
            "final_status": result["final_status"],
            "item_type": result["item_type"],
            "item_id": result["item_id"],
        }

    def _compile_graph(self):
        workflow = StateGraph(ChapterWorkflowState)
        workflow.add_node("build_context", self._build_context)
        workflow.add_node("write", self._write)
        workflow.add_node("review", self._review)
        workflow.add_node("revise", self._revise)
        workflow.add_node("re_review", self._re_review)
        workflow.add_node("persist_approved", self._persist_approved)
        workflow.add_node("persist_candidate", self._persist_candidate)

        workflow.add_edge(START, "build_context")
        workflow.add_edge("build_context", "write")
        workflow.add_edge("write", "review")
        workflow.add_conditional_edges(
            "review",
            self._route_after_review,
            {
                "approved": "persist_approved",
                "revise": "revise",
                "candidate": "persist_candidate",
            },
        )
        workflow.add_conditional_edges(
            "revise",
            self._route_after_revision,
            {"re_review": "re_review", "candidate": "persist_candidate"},
        )
        workflow.add_conditional_edges(
            "re_review",
            self._route_after_re_review,
            {"approved": "persist_approved", "candidate": "persist_candidate"},
        )
        workflow.add_edge("persist_approved", END)
        workflow.add_edge("persist_candidate", END)
        return workflow.compile(checkpointer=False)

    def _build_context(self, state: ChapterWorkflowState) -> ChapterWorkflowState:
        chapter = self._get_chapter(state["chapter_id"])
        base_version_id, base_title, base_goal = chapter.current_version_id, chapter.title, dict(chapter.goal or {})
        workflow_status = self.state_machine.transition(
            chapter.status, CONTEXT_READY
        )
        context = build_chapter_context_from_memory(
            session=self.session,
            chapter_id=chapter.id,
            embedding_client=self.embedding_client,
            embedding_model_name=self.embedding_model_name,
            fallback_embedding_client=self.fallback_embedding_client,
            embedding_fallback_mode=self.embedding_fallback_mode,
        )
        self.session.commit()
        return {"workflow_status": workflow_status, "context": context,
                "base_version_id": base_version_id, "base_title": base_title, "base_goal": base_goal}

    def _lock_current_result(self, state: ChapterWorkflowState) -> Chapter:
        chapter = self._get_chapter(state["chapter_id"])
        if dict(chapter.goal or {}) != state["base_goal"]:
            raise StaleGenerationError("chapter goal changed during generation")
        result = self.session.execute(update(Chapter).where(
            Chapter.id == state["chapter_id"],
            Chapter.claim_token == state["claim_token"],
            Chapter.status == "draft_planned",
            Chapter.current_version_id == state["base_version_id"],
            Chapter.title == state["base_title"],
        ).values(lease_expires_at=utc_now() + timedelta(seconds=LEASE_SECONDS)))
        if result.rowcount != 1:
            raise StaleGenerationError("chapter changed or task ownership expired")
        return chapter

    def _prepare_model_call(self, state: ChapterWorkflowState) -> None:
        self._lock_current_result(state)
        self.session.commit()

    def _write(self, state: ChapterWorkflowState) -> ChapterWorkflowState:
        workflow_status = self.state_machine.transition(
            state["workflow_status"], WRITING
        )
        self._prepare_model_call(state)
        draft = self.writer(state["context"])
        content = draft.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("writer output missing required field: content")
        return {
            "workflow_status": workflow_status,
            "draft": draft,
            "content": content,
        }

    def _review(self, state: ChapterWorkflowState) -> ChapterWorkflowState:
        workflow_status = self.state_machine.transition(
            state["workflow_status"], REVIEW_PENDING
        )
        self._prepare_model_call(state)
        review = self.reviewer(state["content"], state["context"])
        self._extract_review_decision(review)
        return {
            "workflow_status": workflow_status,
            "initial_review": review,
            "final_review": review,
        }

    def _revise(self, state: ChapterWorkflowState) -> ChapterWorkflowState:
        workflow_status = self.state_machine.transition(
            state["workflow_status"], REVISION_PENDING
        )
        initial_review = state["initial_review"]
        self._prepare_model_call(state)
        reviser_result = self.reviser(
            state["content"],
            {
                "issues": initial_review.get("issues", []),
                "revision_priorities": initial_review.get(
                    "revision_priorities", []
                ),
                "summary": initial_review.get("summary") or "",
            },
        )
        revision = dict(reviser_result) if isinstance(reviser_result, dict) else {}
        if not revision:
            return {
                "workflow_status": workflow_status,
                "revision": {},
                "mapping_complete": False,
                "unresolved_issue_ids": [],
            }

        revision["resolved_issue_ids"] = self._normalize_issue_ids(
            revision.get("resolved_issue_ids")
        )
        revision["unresolved_issue_ids"] = self._normalize_issue_ids(
            revision.get("unresolved_issue_ids")
        )
        raw_revised_content = revision.get("revised_content")
        revised_content = (
            raw_revised_content.strip()
            if isinstance(raw_revised_content, str) and raw_revised_content.strip()
            else None
        )
        if revised_content is None:
            revision.pop("revised_content", None)
            return {
                "workflow_status": workflow_status,
                "revision": revision,
                "mapping_complete": False,
                "unresolved_issue_ids": revision["unresolved_issue_ids"],
            }

        revision["revised_content"] = revised_content
        mapping_complete, unresolved_issue_ids = self._issue_mapping(
            initial_review, revision
        )
        return {
            "workflow_status": workflow_status,
            "revision": revision,
            "mapping_complete": mapping_complete,
            "unresolved_issue_ids": unresolved_issue_ids,
        }

    def _re_review(self, state: ChapterWorkflowState) -> ChapterWorkflowState:
        workflow_status = self.state_machine.transition(
            state["workflow_status"], REVIEW_PENDING
        )
        revision = state["revision"]
        self._prepare_model_call(state)
        final_review = self.reviewer(
            revision["revised_content"], state["context"]
        )
        self._extract_review_decision(final_review)
        mapping_complete, unresolved_issue_ids = self._issue_mapping(
            state["initial_review"], revision
        )
        return {
            "workflow_status": workflow_status,
            "final_review": final_review,
            "mapping_complete": mapping_complete,
            "unresolved_issue_ids": unresolved_issue_ids,
        }

    def _persist_approved(
        self, state: ChapterWorkflowState
    ) -> ChapterWorkflowState:
        chapter = self._lock_current_result(state)
        self.state_machine.transition(state["workflow_status"], APPROVED)
        final_review = state["final_review"]
        _, review_score, review_summary, review_state_deltas = (
            self._extract_review_decision(final_review)
        )
        revision = state.get("revision") or {}
        revised_content = revision.get("revised_content")
        is_revised = isinstance(revised_content, str) and bool(
            revised_content.strip()
        )
        review_trace = {
            "initial_review": state["initial_review"],
            "final_review": final_review,
        }
        if is_revised:
            review_trace = {
                "initial_review": state["initial_review"],
                "revision": revision,
                "final_review": final_review,
            }
        draft = state["draft"]
        persist_approved_chapter(
            session=self.session,
            chapter_id=chapter.id,
            content=revised_content if is_revised else state["content"],
            summary=(
                (revision.get("summary") or "") if is_revised
                else draft.get("summary") or ""
            ),
            review_score=review_score,
            state_deltas=(
                review_state_deltas or []
                if is_revised
                else review_state_deltas or draft.get("state_deltas") or []
            ),
            generation_metadata=self._build_generation_metadata(
                context=state["context"], review_trace=review_trace
            ),
            create_publish_job=self.create_publish_job,
        )
        chapter.claim_token = None
        chapter.lease_expires_at = None
        self.session.commit()
        return {
            "final_status": PERSISTED,
            "item_type": "chapter",
            "item_id": chapter.id,
        }

    def _persist_candidate(
        self, state: ChapterWorkflowState
    ) -> ChapterWorkflowState:
        chapter = self._lock_current_result(state)
        if state["workflow_status"] != REVISION_PENDING:
            self.state_machine.transition(
                state["workflow_status"], REVISION_PENDING
            )
        revision = state.get("revision")
        revision_data = revision or {}
        revised_content = revision_data.get("revised_content")
        has_revised_content = isinstance(revised_content, str) and bool(
            revised_content.strip()
        )
        final_review = state["final_review"]
        _, review_score, review_summary, _ = self._extract_review_decision(
            final_review
        )
        unresolved_issue_ids = sorted(set(state.get("unresolved_issue_ids") or []))
        review_trace = {
            "initial_review": state["initial_review"],
            "revision": revision,
            "final_review": final_review,
            "mapping_complete": bool(state.get("mapping_complete", False)),
            "unresolved_issue_ids": unresolved_issue_ids,
        }
        next_goal = dict(chapter.goal or {})
        next_goal["review_trace"] = review_trace
        chapter.goal = next_goal
        persist_review_candidate(
            session=self.session,
            chapter_id=chapter.id,
            content=revised_content if has_revised_content else state["content"],
            summary=(
                (revision_data.get("summary") or "") if has_revised_content
                else state["draft"].get("summary") or ""
            ),
            review_score=review_score,
            generation_metadata={
                **self._build_generation_metadata(context=state["context"], review_trace=review_trace),
                "pending_state_deltas": final_review.get("state_deltas") or [],
            },
        )
        chapter.claim_token = None
        chapter.lease_expires_at = None
        self.session.commit()
        return {
            "final_status": REVISION_PENDING,
            "item_type": "chapter",
            "item_id": chapter.id,
        }

    def _route_after_review(
        self, state: ChapterWorkflowState
    ) -> Literal["approved", "revise", "candidate"]:
        if bool(state["initial_review"].get("approved")):
            return "approved"
        if self.reviser is not None:
            return "revise"
        return "candidate"

    def _route_after_revision(
        self, state: ChapterWorkflowState
    ) -> Literal["re_review", "candidate"]:
        revised_content = (state.get("revision") or {}).get("revised_content")
        if isinstance(revised_content, str) and revised_content.strip():
            return "re_review"
        return "candidate"

    def _route_after_re_review(
        self, state: ChapterWorkflowState
    ) -> Literal["approved", "candidate"]:
        blocking_issue_ids = self._blocking_issue_ids(state["initial_review"])
        unresolved_issue_ids = set(state.get("unresolved_issue_ids") or [])
        can_persist = (
            bool(state["final_review"].get("approved"))
            and bool(state.get("mapping_complete"))
            and not (blocking_issue_ids & unresolved_issue_ids)
        )
        return "approved" if can_persist else "candidate"

    def _get_chapter(self, chapter_id: str) -> Chapter:
        chapter = self.session.scalar(select(Chapter).where(Chapter.id == chapter_id).execution_options(populate_existing=True))
        if chapter is None:
            raise ValueError(f"Chapter not found: {chapter_id}")
        return chapter

    def _extract_review_decision(
        self, review: dict
    ) -> tuple[bool, float, str, list[dict]]:
        if "approved" not in review:
            raise ValueError("reviewer output missing required field: approved")
        if "review_score" not in review:
            raise ValueError("reviewer output missing required field: review_score")
        return (
            review["approved"],
            review["review_score"],
            review.get("summary") or "",
            review.get("state_deltas") or [],
        )

    def _issue_mapping(self, initial_review: dict, revision: dict) -> tuple[bool, list[str]]:
        blocking_issues = [
            issue
            for issue in initial_review.get("issues", [])
            if isinstance(issue, dict) and issue.get("severity") in {"medium", "high"}
        ]
        blocking_ids_present = all(
            isinstance(issue.get("id"), str) and bool(issue["id"].strip())
            for issue in blocking_issues
        )
        blocking_issue_ids = self._blocking_issue_ids(initial_review)
        resolved_issue_ids = set(revision.get("resolved_issue_ids") or [])
        unresolved_issue_ids = set(revision.get("unresolved_issue_ids") or [])
        mapping_complete = blocking_ids_present and blocking_issue_ids <= (
            resolved_issue_ids | unresolved_issue_ids
        )
        return mapping_complete, sorted(unresolved_issue_ids)

    def _blocking_issue_ids(self, initial_review: dict) -> set[str]:
        return {
            issue["id"].strip()
            for issue in initial_review.get("issues", [])
            if isinstance(issue, dict)
            and issue.get("severity") in {"medium", "high"}
            and isinstance(issue.get("id"), str)
            and issue["id"].strip()
        }

    def _normalize_issue_ids(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(
            dict.fromkeys(
                item.strip()
                for item in value
                if isinstance(item, str) and item.strip()
            )
        )

    def _build_generation_metadata(self, *, context: dict, review_trace: dict) -> dict:
        return {
            "planning_trace": (context.get("chapter_goal") or {}).get(
                "planning_trace"
            ),
            "retrieval_evidence": context.get("retrieval_evidence", []),
            "retrieval_mode": context.get("retrieval_mode", "none"),
            "retrieval_degradation_reason": context.get(
                "retrieval_degradation_reason"
            ),
            "review_trace": review_trace,
        }
