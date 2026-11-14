from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.review_issues import normalize_review_issues
from app.api import routes_trends
from app.api.routes_chapters import create_chapters_from_outline
from app.api.routes_trends import (
    DirectionGenerateRequest,
    create_trend_capture,
    generate_direction,
)
from app.db.base import Base
from app.db.models import Book, Chapter, ChapterVersion, PublishJob, SchedulerRun
from app.db.repositories import BookRepository
from app.orchestrator.scheduler import Scheduler
from app.orchestrator.scheduler_runner import run_scheduler_loop
from app.services.embedding import KeywordEmbeddingClient


class FakeSemanticEmbeddingClient:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def test_radar_direction_outline_chapter_review_publish_workflow(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            Book(
                id="book_e2e",
                title="逆风翻盘",
                platform="fanqie",
                genre="urban",
            )
        )
        session.commit()

        monkeypatch.setattr(
            routes_trends,
            "fetch_hotlists",
            lambda: {
                "providers": [
                    {
                        "provider": "fanqie",
                        "provider_label": "番茄小说",
                        "source_url": "https://fanqienovel.com/rank",
                        "status": "real",
                        "error": None,
                        "items": [
                            {
                                "id": "fanqie:1",
                                "provider": "fanqie",
                                "rank": 1,
                                "title": "逆风翻盘",
                                "author": "林一",
                                "category": "都市",
                                "url": "https://fanqienovel.com/page/1001",
                                "raw_metadata": {},
                            }
                        ],
                        "reference_samples": [],
                    }
                ]
            },
        )
        capture = create_trend_capture(session=session)
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    direction_generator=lambda payload: {
                        "direction_name": payload["direction_name"],
                        "acts": ["入局", "受压", "破局"],
                        "chapter_seed_plan": [
                            {
                                "chapter": 1,
                                "title": "仓库门锁",
                                "goal": "进入仓库并恢复门锁线索",
                                "must_include": ["钥匙"],
                                "must_avoid": [],
                                "target_words": 20,
                            }
                        ],
                    }
                )
            )
        )
        direction = generate_direction(
            DirectionGenerateRequest(
                direction_name="都市逆袭成长",
                selected_platforms=["番茄"],
                keywords=["都市", "逆袭"],
                capture_id=capture["capture_id"],
                selected_item_ids=["fanqie:1"],
            ),
            request=request,
            session=session,
        )
        source_trace = direction["source_trace"]
        assert source_trace["capture_id"] == capture["capture_id"]
        assert source_trace["selected_item_ids"] == ["fanqie:1"]

        BookRepository(session).update_outline(
            "book_e2e",
            book_outline={
                "direction_source": source_trace,
                "acts": direction["acts"],
                "chapter_seed_plan": direction["chapter_seed_plan"],
            },
            logline="小人物借一次异常订单逆势翻盘。",
            style_rules={},
        )

        created = create_chapters_from_outline(book_id="book_e2e", session=session)
        chapter_id = created["items"][0]["id"]
        assert (
            created["items"][0]["goal"]["planning_trace"]["direction_source"]
            == source_trace
        )

        issue = normalize_review_issues(
            [
                {
                    "type": "continuity",
                    "severity": "high",
                    "message": "门锁线索缺失",
                    "evidence": "上一章门已锁死",
                    "source": "model",
                }
            ]
        )[0]
        issue_id = issue["id"]
        reviews = iter(
            [
                {
                    "approved": False,
                    "review_score": 70,
                    "issues": [issue],
                    "revision_priorities": ["恢复门锁因果"],
                    "state_deltas": [],
                },
                {
                    "approved": True,
                    "review_score": 94,
                    "issues": [],
                    "revision_priorities": [],
                    "state_deltas": [],
                },
            ]
        )
        revision = {
            "revised_content": "主角用上一章取得的钥匙打开仓库门。",
            "resolved_issue_ids": [issue_id],
            "unresolved_issue_ids": [],
            "change_summary": [
                {"issue_id": issue_id, "change": "补回钥匙与门锁因果"}
            ],
        }
        published = []

        def publish_draft(title: str, content: str) -> dict:
            published.append((title, content))
            return {
                "success": True,
                "platform_draft_id": "draft_e2e",
                "screenshot_path": None,
            }

        scheduler = Scheduler(
            session=session,
            writer=lambda context: {
                "content": "主角进入仓库，但遗漏门锁线索。",
                "summary": "进入仓库",
            },
            reviewer=lambda content, context: next(reviews),
            reviser=lambda content, instructions: revision,
            publisher=publish_draft,
            embedding_client=FakeSemanticEmbeddingClient(),
            fallback_embedding_client=KeywordEmbeddingClient(),
            embedding_model_name="fake-semantic",
            embedding_fallback_mode="keyword",
        )

        worker_events = []
        worker_summary = run_scheduler_loop(
            scheduler=scheduler,
            batch_limit=1,
            poll_interval_seconds=0,
            max_cycles=2,
            sleep_fn=lambda seconds: None,
            session=session,
            event_callback=lambda event, payload: worker_events.append((event, payload)),
        )

        assert worker_summary["processed"] == 2
        runs = list(
            session.scalars(select(SchedulerRun).order_by(SchedulerRun.cycle_number))
        )
        versions = list(
            session.scalars(
                select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id)
            )
        )
        jobs = list(
            session.scalars(
                select(PublishJob).where(PublishJob.chapter_id == chapter_id)
            )
        )
        chapter = session.scalar(select(Chapter).where(Chapter.id == chapter_id))
        assert len(versions) == 1
        assert len(jobs) == 1
        version = versions[0]
        job = jobs[0]
        assert chapter is not None
        assert chapter.current_version_id == version.id
        assert [run.processed_count for run in runs] == [1, 1]
        assert [run.outcome_counts for run in runs] == [
            {"persisted": 1},
            {"draft_uploaded": 1},
        ]
        assert runs[0].outcomes == [
            {
                "final_status": "persisted",
                "item_type": "chapter",
                "item_id": chapter_id,
            }
        ]
        assert runs[1].outcomes == [
            {
                "final_status": "draft_uploaded",
                "item_type": "publish_job",
                "item_id": job.id,
            }
        ]
        assert [
            payload["cycle_number"]
            for event, payload in worker_events
            if event == "worker.cycle.completed"
        ] == [1, 2]

        assert job.status == "draft_uploaded"
        assert job.platform_draft_id == "draft_e2e"
        assert version.content == revision["revised_content"]
        assert published == [("仓库门锁", revision["revised_content"])]
        assert (
            version.generation_metadata["planning_trace"]["direction_source"]
            == source_trace
        )
        assert version.generation_metadata["retrieval_mode"] == "semantic"
        assert version.generation_metadata["retrieval_evidence"]
        review_trace = version.generation_metadata["review_trace"]
        assert review_trace["initial_review"]["issues"][0]["id"] == issue_id
        assert review_trace["revision"]["resolved_issue_ids"] == [issue_id]
        assert review_trace["final_review"]["approved"] is True
