import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app import launch


@pytest.fixture
def configured_worker(monkeypatch):
    monkeypatch.setattr(launch, "settings", SimpleNamespace(
        openai_api_key="YOUR_API_KEY_HERE", openai_base_url="https://example.invalid",
        openai_model="writer", openai_review_model="reviewer",
    ))


def test_open_creation_page_opens_ops_url(monkeypatch) -> None:
    opened_urls: list[str] = []

    monkeypatch.setattr(launch.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(launch.webbrowser, "open", opened_urls.append)

    launch.open_creation_page()
    launch.open_creation_page(host="127.0.0.1", port=8000)

    assert opened_urls == ["http://127.0.0.1:8888/ops", "http://127.0.0.1:8000/ops"]


def test_main_defaults_to_web_mode(monkeypatch) -> None:
    calls: list[tuple[str, str, int]] = []

    monkeypatch.setattr(
        launch,
        "run_web",
        lambda *, host, port: calls.append(("web", host, port)),
    )
    monkeypatch.setattr(
        launch,
        "run_worker",
        lambda: calls.append(("worker", "", 0)),
    )

    launch.main([])

    assert calls == [("web", "127.0.0.1", 8888)]


def test_main_allows_web_cli_host_and_port_overrides(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(
        launch,
        "run_web",
        lambda *, host, port, open_browser: calls.append((host, port, open_browser)),
    )

    launch.main(["web", "--host", "0.0.0.0", "--port", "9100"])

    assert calls == [("0.0.0.0", 9100, True)]


def test_main_web_no_browser_starts_only_uvicorn(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(launch.threading, "Thread", lambda **kwargs: pytest.fail("browser thread created"))
    monkeypatch.setattr(launch.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    assert launch.main(["web", "--no-browser"]) == 0
    assert calls == [("app.main:app", {"host": "127.0.0.1", "port": 8888, "reload": False})]


def test_main_migrate_only_initializes_database(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(launch, "initialize_database", lambda: calls.append("migrate"))
    monkeypatch.setattr(launch, "run_web", lambda **kwargs: pytest.fail("web started"))
    monkeypatch.setattr(launch, "run_worker", lambda **kwargs: pytest.fail("worker started"))

    assert launch.main(["migrate"]) == 0
    assert calls == ["migrate"]


def test_main_worker_mode_runs_scheduler_loop(monkeypatch) -> None:
    worker_calls: list[dict] = []

    monkeypatch.setattr(
        launch,
        "run_web",
        lambda *, host, port: worker_calls.append({"mode": "web", "host": host, "port": port}),
    )
    monkeypatch.setattr(
        launch,
        "run_worker",
        lambda **kwargs: worker_calls.append({"mode": "worker", **kwargs}),
    )

    launch.main(["worker"])

    assert worker_calls == [
        {
            "mode": "worker",
            "batch_limit": None,
            "poll_interval_seconds": None,
            "max_cycles": None,
            "max_consecutive_failures": 5,
        }
    ]


def test_main_allows_worker_cli_overrides(monkeypatch) -> None:
    calls: list[dict] = []

    monkeypatch.setattr(
        launch,
        "run_worker",
        lambda **kwargs: calls.append(kwargs),
    )

    launch.main(
        [
            "worker",
            "--batch-limit",
            "8",
            "--poll-interval",
            "2.5",
            "--max-cycles",
            "4",
            "--max-consecutive-failures",
            "2",
        ]
    )

    assert calls == [
        {
            "batch_limit": 8,
            "poll_interval_seconds": 2.5,
            "max_cycles": 4,
            "max_consecutive_failures": 2,
        }
    ]


@pytest.mark.parametrize(("host", "token"), [
    ("localhost", ""), ("127.0.0.1", ""), ("::1", ""), ("0.0.0.0", "YOUR_TOKEN_HERE"),
])
def test_run_web_starts_browser_thread_and_uvicorn(monkeypatch, host, token) -> None:
    monkeypatch.setattr(launch.settings, "access_token", SecretStr(token))
    thread_calls: list[dict] = []
    uvicorn_calls = []

    class FakeThread:
        def __init__(self, *, target, kwargs, daemon) -> None:
            thread_calls.append(
                {
                    "target": target,
                    "kwargs": kwargs,
                    "daemon": daemon,
                }
            )

        def start(self) -> None:
            thread_calls.append({"started": True})

    monkeypatch.setattr(launch.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        launch.uvicorn,
        "run",
        lambda app_path, host, port, reload: uvicorn_calls.append(
            (app_path, host, port, reload)
        ),
    )

    assert launch.run_web(host=host, port=9000) == 0

    assert thread_calls[0]["target"] is launch.open_creation_page
    assert thread_calls[0]["kwargs"] == {"host": host, "port": 9000}
    assert thread_calls[0]["daemon"] is True
    assert thread_calls[1] == {"started": True}
    assert uvicorn_calls == [("app.main:app", host, 9000, False)]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "127.0.0.2"])
def test_web_rejects_non_local_host_before_starting(monkeypatch, capsys, host):
    monkeypatch.setattr(launch.settings, "access_token", SecretStr(""))
    monkeypatch.setattr(launch.threading, "Thread", lambda **kwargs: pytest.fail("browser started"))
    monkeypatch.setattr(launch.uvicorn, "run", lambda *args, **kwargs: pytest.fail("server started"))
    assert launch.main(["web", "--host", host]) == 1
    assert "ACCESS_TOKEN is required" in capsys.readouterr().err


def test_run_worker_builds_application_and_runs_scheduler_loop(monkeypatch) -> None:
    scheduler = object()
    session = object()
    app_instance = SimpleNamespace(scheduler=scheduler, session=session)
    loop_calls: list[object] = []
    info_events: list[tuple[str, dict]] = []
    worker_settings = SimpleNamespace(
        openai_api_key="YOUR_API_KEY_HERE",
        openai_base_url="https://example.invalid",
        openai_model="writer",
        openai_review_model="reviewer",
        worker_batch_limit=7,
        worker_poll_interval_seconds=1.5,
        worker_max_cycles=3,
    )

    monkeypatch.setattr(launch, "settings", worker_settings)

    monkeypatch.setattr(
        launch,
        "build_worker",
        lambda settings: app_instance,
    )
    monkeypatch.setattr(
        launch,
        "run_scheduler_loop",
        lambda *, scheduler, batch_limit, poll_interval_seconds, max_cycles, max_consecutive_failures, session, event_callback: (
            loop_calls.append(
                {
                    "scheduler": scheduler,
                    "batch_limit": batch_limit,
                    "poll_interval_seconds": poll_interval_seconds,
                    "max_cycles": max_cycles,
                    "max_consecutive_failures": max_consecutive_failures,
                    "session": session,
                    "event_callback": event_callback,
                }
            )
            or event_callback("worker.cycle.completed", {"cycle_number": 1})
            or {
                "cycles": 3,
                "processed": 5,
                "status_counts": {"processed": 2, "failed": 1, "idle": 0},
            }
        ),
    )
    monkeypatch.setattr(
        launch.webbrowser,
        "open",
        lambda url: (_ for _ in ()).throw(AssertionError("browser should not open")),
    )
    monkeypatch.setattr(
        launch.uvicorn,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("uvicorn should not run")
        ),
    )
    monkeypatch.setattr(
        launch.logger,
        "info",
        lambda message, extra=None: info_events.append((message, extra or {})),
    )

    launch.run_worker()

    assert loop_calls == [
        {
            "scheduler": scheduler,
            "batch_limit": 7,
            "poll_interval_seconds": 1.5,
            "max_cycles": 3,
            "max_consecutive_failures": 5,
            "session": session,
            "event_callback": loop_calls[0]["event_callback"],
        }
    ]
    assert info_events == [
        (
            "worker.start",
            {
                "batch_limit": 7,
                "poll_interval_seconds": 1.5,
                "max_cycles": 3,
            },
        ),
        (
            "worker.cycle.completed",
            {"cycle_number": 1},
        ),
        (
            "worker.completed",
            {
                "cycles": 3,
                "processed": 5,
                "status_counts": {"processed": 2, "failed": 1, "idle": 0},
            },
        ),
    ]


def test_run_worker_allows_explicit_overrides(monkeypatch, configured_worker) -> None:
    scheduler = object()
    session = object()
    app_instance = SimpleNamespace(scheduler=scheduler, session=session)
    loop_calls: list[dict] = []

    monkeypatch.setattr(
        launch,
        "build_worker",
        lambda settings: app_instance,
    )
    monkeypatch.setattr(
        launch,
        "run_scheduler_loop",
        lambda *, scheduler, batch_limit, poll_interval_seconds, max_cycles, max_consecutive_failures, session, event_callback: (
            loop_calls.append(
                {
                    "scheduler": scheduler,
                    "batch_limit": batch_limit,
                    "poll_interval_seconds": poll_interval_seconds,
                    "max_cycles": max_cycles,
                    "max_consecutive_failures": max_consecutive_failures,
                    "session": session,
                    "event_callback": event_callback,
                }
            )
            or {"cycles": 1, "processed": 2}
        ),
    )
    monkeypatch.setattr(launch.logger, "info", lambda *args, **kwargs: None)

    launch.run_worker(batch_limit=11, poll_interval_seconds=0.25, max_cycles=9, max_consecutive_failures=2)

    assert loop_calls == [
        {
            "scheduler": scheduler,
            "batch_limit": 11,
            "poll_interval_seconds": 0.25,
            "max_cycles": 9,
            "max_consecutive_failures": 2,
            "session": session,
            "event_callback": loop_calls[0]["event_callback"],
        }
    ]


def test_run_worker_logs_fatal_and_returns_non_zero(monkeypatch, configured_worker) -> None:
    error_events: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        launch,
        "build_worker",
        lambda settings: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        launch.logger,
        "error",
        lambda message, *args, extra=None, exc_info=False: error_events.append(
            (message, extra or {}, exc_info)
        ),
    )

    exit_code = launch.run_worker()

    assert exit_code == 1
    assert error_events == [("worker.fatal", {"error_type": "RuntimeError"}, True)]


def test_run_worker_logs_keyboard_interrupt_shutdown_and_returns_zero(monkeypatch, configured_worker) -> None:
    info_events: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        launch,
        "build_worker",
        lambda settings: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        launch.logger,
        "info",
        lambda message, extra=None: info_events.append((message, extra or {})),
    )

    exit_code = launch.run_worker()

    assert exit_code == 0
    assert info_events == [("worker.shutdown", {"reason": "keyboard_interrupt"})]


def test_run_worker_logs_structured_lifecycle_events(monkeypatch, configured_worker):
    scheduler = object()
    session = object()
    app_instance = SimpleNamespace(scheduler=scheduler, session=session)
    log_events = []

    monkeypatch.setattr(
        launch,
        "build_worker",
        lambda settings: app_instance,
    )
    monkeypatch.setattr(
        launch,
        "run_scheduler_loop",
        lambda **kwargs: {
            "cycles": 2,
            "processed": 3,
            "status_counts": {"processed": 1, "failed": 1, "idle": 0},
        },
    )
    monkeypatch.setattr(
        launch.logger,
        "info",
        lambda message, extra=None: log_events.append((message, extra or {})),
    )

    exit_code = launch.run_worker(batch_limit=5, poll_interval_seconds=1.0, max_cycles=2)

    assert exit_code == 0
    assert log_events[0][0] == "worker.start"
    assert log_events[0][1]["batch_limit"] == 5
    assert log_events[1][0] == "worker.completed"
    assert log_events[1][1]["status_counts"]["failed"] == 1


@pytest.mark.parametrize(
    ("argv", "expected_message"),
    [
        (
            ["worker", "--batch-limit", "0"],
            "argument --batch-limit: must be a positive integer",
        ),
        (
            ["worker", "--poll-interval", "0"],
            "argument --poll-interval: must be a positive number",
        ),
        (
            ["worker", "--max-cycles", "-1"],
            "argument --max-cycles: must be a positive integer",
        ),
    ],
)
def test_main_rejects_invalid_worker_cli_values(argv, expected_message, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        launch.main(argv)

    captured = capsys.readouterr()

    assert exc_info.value.code == 2
    assert expected_message in captured.err


def test_worker_without_key_leaves_queued_chapters_untouched(tmp_path):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.db.models import Book, Chapter, SchedulerRun
    from app.db.schema_migrations import upgrade_database

    database_url = f"sqlite+pysqlite:///{tmp_path / 'queue.db'}"
    engine = create_engine(database_url)
    try:
        upgrade_database(engine)
        with Session(engine) as session:
            session.add(Book(id="book", title="Book", platform="local", genre="fiction"))
            session.flush()
            session.add_all([
                Chapter(id=f"chapter_{n}", book_id="book", chapter_number=n, title="Draft",
                        status="draft_planned", attempt_count=n)
                for n in (0, 1)
            ])
            session.commit()
        process = subprocess.run(
            [sys.executable, "-m", "app.launch", "worker", "--max-cycles", "1"],
            env={**os.environ, "DATABASE_URL": database_url, "OPENAI_API_KEY": "",
                 "OPENAI_BASE_URL": "https://example.invalid", "OPENAI_MODEL": "writer",
                 "OPENAI_REVIEW_MODEL": "reviewer", "LOG_LEVEL": "INFO"},
            capture_output=True, timeout=30,
        )
        assert process.returncode == 1
        assert b"OPENAI_API_KEY is required" in process.stderr
        assert b"worker.cycle.started" not in process.stderr
        with Session(engine) as session:
            rows = session.execute(select(Chapter.status, Chapter.attempt_count, Chapter.claim_token)
                                   .order_by(Chapter.chapter_number)).all()
            assert rows == [("draft_planned", 0, None), ("draft_planned", 1, None)]
            assert session.scalar(select(SchedulerRun.id)) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("log_level", ["INFO", "warning"])
def test_worker_cli_console_events_follow_log_level(log_level):
    placeholder = "YOUR_API_KEY_HERE"
    environment = {
        **os.environ,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "OPENAI_API_KEY": placeholder,
        "OPENAI_BASE_URL": "http://127.0.0.1:1",
        "EMBEDDING_API_KEY": placeholder,
        "EMBEDDING_BASE_URL": "http://127.0.0.1:1",
        "PUBLISH_MODE": "",
        "LOG_LEVEL": log_level,
    }
    process = subprocess.run(
        [sys.executable, "-m", "app.launch", "worker", "--max-cycles", "1"],
        env=environment,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
    )
    assert process.returncode == 0
    output = process.stdout + process.stderr
    assert placeholder not in output
    if log_level == "INFO":
        start = next(line for line in output.splitlines() if "worker.start" in line)
        completed = next(line for line in output.splitlines() if "worker.cycle.completed" in line)
        assert "max_cycles=1" in start
        assert "cycle_number=1" in completed
        assert "status=idle" in completed
        assert "reason_code=no_pending_work" in completed
    else:
        assert "worker.start" not in output
        assert "worker.cycle.completed" not in output


def test_worker_event_formatter_excludes_raw_errors_and_outcomes():
    placeholder = "YOUR_API_KEY_HERE"
    record = logging.makeLogRecord({
        "name": launch.logger.name,
        "msg": "worker.cycle.failed",
        "cycle_number": 7,
        "error": f"Authorization: Bearer {placeholder}",
        "outcomes": [{"response": placeholder}],
        "api_key": placeholder,
    })
    output = launch.WorkerEventFormatter().format(record)

    assert "worker.cycle.failed" in output
    assert "cycle_number=7" in output
    assert placeholder not in output


def test_worker_cycle_failed_log_includes_error_type():
    record = logging.makeLogRecord({
        "name": launch.logger.name,
        "msg": "worker.cycle.failed",
        "cycle_number": 3,
        "error_type": "OperationalError",
    })
    output = launch.WorkerEventFormatter().format(record)

    assert "error_type=OperationalError" in output
