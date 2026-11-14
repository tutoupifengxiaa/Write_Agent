from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser

import uvicorn

from app.application import build_worker, initialize_database
from app.config import secret_value, settings
from app.orchestrator.scheduler_runner import DEFAULT_MAX_CONSECUTIVE_FAILURES, run_scheduler_loop
from app.services.llm_client import LLMConfigurationError, validate_worker_settings

logger = logging.getLogger(__name__)
WORKER_LOG_FIELDS = (
    "batch_limit", "poll_interval_seconds", "max_cycles", "cycle_number", "status",
    "reason_code", "duration_ms", "processed_count", "outcome_counts", "cycles",
    "processed", "status_counts", "reason", "error_type",
)


class WorkerEventFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if record.name != logger.name:
            return message
        fields = [f"{key}={record.__dict__[key]}" for key in WORKER_LOG_FIELDS if key in record.__dict__]
        return " ".join([message, *fields])


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def open_creation_page(host: str = "127.0.0.1", port: int = 8888) -> None:
    time.sleep(0.8)
    webbrowser.open(f"http://{host}:{port}/ops")


def _log_worker_event(event: str, **payload) -> None:
    logger.info(event, extra={key: payload[key] for key in WORKER_LOG_FIELDS if key in payload})


def run_web(*, host: str = "127.0.0.1", port: int = 8888, open_browser: bool = True) -> int:
    has_token = bool(secret_value(settings.access_token).strip())
    if host.lower() not in {"127.0.0.1", "localhost", "::1"} and not has_token:
        print("ACCESS_TOKEN is required when listening on a non-local address", file=sys.stderr)
        return 1
    if open_browser:
        threading.Thread(
            target=open_creation_page,
            kwargs={"host": host, "port": port},
            daemon=True,
        ).start()
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
    return 0


def run_worker(
    *,
    batch_limit: int | None = None,
    poll_interval_seconds: float | None = None,
    max_cycles: int | None = None,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
) -> int:
    resolved_batch_limit = (
        getattr(settings, "worker_batch_limit", 10) if batch_limit is None else batch_limit
    )
    resolved_poll_interval = (
        getattr(settings, "worker_poll_interval_seconds", 5.0)
        if poll_interval_seconds is None
        else poll_interval_seconds
    )
    resolved_max_cycles = (
        getattr(settings, "worker_max_cycles", None) if max_cycles is None else max_cycles
    )

    application = None
    try:
        try:
            validate_worker_settings(settings)
        except LLMConfigurationError as exc:
            logger.error("worker.configuration_error %s", exc)
            return 1
        application = build_worker(settings)
        _log_worker_event(
            "worker.start",
            batch_limit=resolved_batch_limit,
            poll_interval_seconds=resolved_poll_interval,
            max_cycles=resolved_max_cycles,
        )
        result = run_scheduler_loop(
            scheduler=application.scheduler,
            batch_limit=resolved_batch_limit,
            poll_interval_seconds=resolved_poll_interval,
            max_cycles=resolved_max_cycles,
            session=application.session,
            max_consecutive_failures=max_consecutive_failures,
            event_callback=lambda event, payload: _log_worker_event(event, **payload),
        )
        _log_worker_event("worker.completed", **result)
        return 0
    except KeyboardInterrupt:
        _log_worker_event("worker.shutdown", reason="keyboard_interrupt")
        return 0
    except Exception as exc:
        logger.error("worker.fatal", extra={"error_type": type(exc).__name__}, exc_info=True)
        return 1
    finally:
        close = getattr(application, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.error("worker.close_failed")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.launch")
    subparsers = parser.add_subparsers(dest="mode")
    subparsers.add_parser("migrate")

    web_parser = subparsers.add_parser("web")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=_positive_int, default=8888)
    web_parser.add_argument("--no-browser", action="store_true", help="start the server without opening a browser")

    worker_parser = subparsers.add_parser("worker")
    worker_parser.add_argument("--batch-limit", type=_positive_int)
    worker_parser.add_argument("--poll-interval", type=_positive_float)
    worker_parser.add_argument("--max-cycles", type=_positive_int)
    worker_parser.add_argument(
        "--max-consecutive-failures", type=_positive_int, default=DEFAULT_MAX_CONSECUTIVE_FAILURES
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    handler = logging.StreamHandler()
    handler.setFormatter(WorkerEventFormatter())
    logging.basicConfig(handlers=[handler])
    logger.setLevel(settings.log_level.upper())
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return run_web(host="127.0.0.1", port=8888)

    parser = _build_parser()
    parsed = parser.parse_args(args)
    mode = parsed.mode or "web"

    if mode == "migrate":
        initialize_database()
        return 0
    if mode == "web":
        return run_web(host=parsed.host, port=parsed.port, open_browser=not parsed.no_browser)
    if mode == "worker":
        return run_worker(
            batch_limit=parsed.batch_limit,
            poll_interval_seconds=parsed.poll_interval,
            max_cycles=parsed.max_cycles,
            max_consecutive_failures=parsed.max_consecutive_failures,
        )

    raise ValueError(f"unknown launch mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
