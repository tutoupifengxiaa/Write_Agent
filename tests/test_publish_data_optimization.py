import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes_jobs import get_session, jobs_health, router
from app.db.base import Base
from app.db.models import Book, Chapter, PublishJob, SchedulerRun
from app.db.repositories import PublishJobRepository, SchedulerRunRepository
from app.db.schema_migrations import upgrade_database


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Book(id=b, title=b, platform="stub", genre="test") for b in ("b1", "b2")])
        session.flush()
        session.add_all([Chapter(id=c, book_id=b, chapter_number=n, title=c)
                         for c, b, n in (("c1", "b1", 1), ("c2", "b1", 2), ("c3", "b2", 1))])
        session.commit()
        yield session
    engine.dispose()


def add_job(session, job_id, chapter_id="c1", created_at=None, status="publish_pending"):
    job = PublishJob(id=job_id, chapter_id=chapter_id, platform="stub", mode="draft", status=status)
    # Assignment also lets the pre-change model reach the ordering assertion.
    job.created_at = created_at
    session.add(job)
    session.flush()
    return job


def test_latest_job_uses_time_instead_of_random_id(session):
    add_job(session, "z_old", created_at=datetime(2026, 9, 8, 10, 0, 0, 1))
    add_job(session, "a_new", created_at=datetime(2026, 9, 8, 10, 0, 0, 2))
    assert PublishJobRepository(session).get_latest_for_chapter("c1").id == "a_new"


def test_new_orm_job_has_utc_microsecond_timestamp(session):
    before = datetime.now(timezone.utc)
    job = PublishJobRepository(session).create_job(chapter_id="c1", platform="stub", mode="draft", status="publish_pending")
    after = datetime.now(timezone.utc)
    created_at = getattr(job, "created_at", None)
    assert created_at is not None
    assert before <= created_at.replace(tzinfo=timezone.utc) <= after


def test_old_null_history_is_stable_and_default_list_order_stays_compatible(session):
    add_job(session, "z_legacy")
    add_job(session, "a_legacy")
    add_job(session, "b_new", created_at=datetime(2026, 9, 8))
    repo = PublishJobRepository(session)
    assert [job.id for job in repo.list_jobs()] == ["a_legacy", "b_new", "z_legacy"]
    assert [job.id for job in repo.list_jobs(descending=True)] == ["b_new", "z_legacy", "a_legacy"]


def test_migration_preserves_unknown_legacy_time_and_is_repeatable():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE publish_jobs (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO publish_jobs (id) VALUES ('legacy')"))
    upgrade_database(engine)
    columns = {column["name"]: column for column in inspect(engine).get_columns("publish_jobs")}
    assert "created_at" in columns
    assert columns["created_at"]["nullable"] is True
    with engine.connect() as connection:
        assert connection.execute(text("SELECT created_at FROM publish_jobs")).scalar() is None
    upgrade_database(engine)
    engine.dispose()


def test_jobs_api_batches_latest_by_book_before_limit(session):
    add_job(session, "z_old", created_at=datetime(2026, 9, 7))
    add_job(session, "a_new", created_at=datetime(2026, 9, 8))
    add_job(session, "b_second", "c2", datetime(2026, 9, 6))
    add_job(session, "other", "c3", datetime(2026, 9, 9))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        result = client.get("/jobs?book_id=b1&latest_per_chapter=true&limit=2")
        assert result.status_code == 200
        assert [row["id"] for row in result.json()] == ["a_new", "b_second"]
        assert all(row.get("created_at") for row in result.json())
        assert client.get("/jobs?book_id=b1&latest_per_chapter=true&limit=1").json()[0]["id"] == "a_new"
        assert len(client.get("/jobs?limit=1").json()) == 1
        assert client.get("/jobs?limit=0").status_code == 422
        assert client.get("/jobs?book_id=missing&latest_per_chapter=true").json() == []
        assert client.get("/jobs/a_new").json()["created_at"]


def test_summary_and_health_use_aggregate_queries_without_loading_rows(session):
    session.add_all([SchedulerRun(cycle_number=i, processed_count=i, status=status, outcomes=[])
                     for i, status in enumerate(("processed", "idle", "attention_required", "failed", "other"), 1)])
    add_job(session, "pending")
    session.commit()
    queries = []
    def record_sql(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement.lower())
    event.listen(session.bind, "before_cursor_execute", record_sql)
    try:
        assert SchedulerRunRepository(session).get_summary() == {
            "total_runs": 5, "processed_runs": 1, "idle_runs": 1,
            "attention_required_runs": 1, "failed_runs": 1, "total_processed_items": 15,
        }
        assert jobs_health(session) == {"scheduler": "busy", "pending_chapters": 3, "pending_publish_jobs": 1}
    finally:
        event.remove(session.bind, "before_cursor_execute", record_sql)
    assert len(queries) <= 3
    assert all("count(" in query for query in queries)
    assert not any(".outcomes" in query for query in queries)


def test_empty_summary_and_health_return_zeroes(session):
    assert set(SchedulerRunRepository(session).get_summary().values()) == {0}
    assert jobs_health(session)["pending_publish_jobs"] == 0


@pytest.mark.parametrize("page", ["library", "ops"])
def test_page_async_loading_contracts(page):
    """Execute the real UI handlers with deterministic deferred HTTP responses."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for UI async contract checks")
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const page = process.argv[1], path = process.argv[2];
const state = [], effects = [], requests = [];
let cursor = 0, App, tree;
const react = {
  useState(initial) {
    const index = cursor++;
    if (!(index in state)) state[index] = initial;
    return [state[index], value => { state[index] = typeof value === 'function' ? value(state[index]) : value; }];
  },
  useRef(initial) {
    const index = cursor++;
    if (!(index in state)) state[index] = { current: initial };
    return state[index];
  },
  useEffect(fn) { if (!App) effects.push(fn); },
  createElement(type, props, ...children) {
    if (typeof type === 'function') {
      const result = type({...props, children: children.length <= 1 ? children[0] : children});
      if (type.name === 'App') App = type;
      return result;
    }
    return { type, props: props || {}, children };
  }
};
function request(url, options, kind) {
  return new Promise((resolve, reject) => requests.push({url, options, kind, resolve, reject}));
}
const sandbox = { window: { React: react, ReactDOM: { createRoot: () => ({render: value => tree = value}) } },
  document: { getElementById: () => ({}) }, console,
  // The shared nav asks once whether to show logout; answer it here so page request counts stay unchanged.
  fetch: (url, ...rest) => url === '/auth/status'
    ? Promise.resolve({ ok: true, json: () => Promise.resolve({ enabled: false }) })
    : request(url, ...rest) };
const commonPath = require('path').join(require('path').dirname(path), 'common.js');
vm.runInNewContext(fs.readFileSync(commonPath, 'utf8'), sandbox);
Object.assign(sandbox.window.WriteAgentUI, { fetchJson: request, requestJson: (url, options) => request(url, options, 'shared') });
vm.runInNewContext(fs.readFileSync(path, 'utf8'), sandbox);
effects.forEach(fn => fn());
function render() { cursor = 0; tree = App(); }
async function tick() { for (let i = 0; i < 8; i++) await Promise.resolve(); render(); }
function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(x => nodes(x));
  if (!node || typeof node !== 'object') return [];
  return [node, ...nodes(node.children)];
}
function content(node) {
  if (Array.isArray(node)) return node.map(content).join(' ');
  return node && typeof node === 'object' ? content(node.children) : String(node || '');
}
(async () => {
  if (page === 'library') {
    requests[0].resolve([{id:'b1', title:'Book One'}, {id:'b2', title:'Book Two'}]);
    await tick();
    assert.equal(requests.length, 4);
    nodes(tree).find(n => n.type === 'button' && content(n).includes('Book Two')).props.onClick();
    assert.equal(requests.length, 7);
    requests.find(r => r.url === '/books/b2').resolve({id:'b2', title:'Book Two'});
    requests.find(r => r.url === '/books/b2/chapters').resolve([{id:'c2', title:'Chapter Two', chapter_number:1, status:'draft_planned'}]);
    requests.find(r => r.url === '/jobs?book_id=b2&latest_per_chapter=true').resolve([{id:'j2',chapter_id:'c2',status:'publish_failed'}]);
    await tick();
    requests.find(r => r.url === '/books/b1').resolve({id:'b1', title:'Book One'});
    requests.find(r => r.url === '/books/b1/chapters').resolve([]);
    requests.find(r => r.url === '/jobs?book_id=b1&latest_per_chapter=true').reject(new Error('stale failure'));
    await tick();
    assert(nodes(tree).some(n => n.type === 'h2' && content(n) === 'Book Two'));
    assert(!content(tree).includes('stale failure'));
    assert.equal(requests.filter(r => r.url.startsWith('/jobs?book_id=')).length, 2);
    assert(!requests.some(r => r.url.includes('/jobs?chapter_id=')));
    assert.equal(content(nodes(tree).find(n => n.type === 'option' && n.props.value === 'draft_planned')), '待写作');
    for (const [label, url] of [['新建章节','/chapters'], ['按故事线规划','/chapters/plan'], ['重试发布','/chapters/c2/retry-publish'], ['归档失败','/chapters/c2/archive-publish-failure']]) {
      nodes(tree).find(n => n.type === 'button' && content(n) === label).props.onClick();
      const write = requests[requests.length - 1];
      assert.equal(write.url, url);
      assert.equal(write.kind, 'shared');
      assert.equal(write.options.method, 'POST');
      write.reject(new Error('服务端具体原因'));
      await tick();
      assert(content(tree).includes('服务端具体原因'));
    }
  } else {
    assert.equal(requests.length, 6);
    requests.forEach(r => {
      if (r.url === '/jobs/runs/summary') r.reject(new Error('summary unavailable'));
      else if (r.url === '/jobs/health') r.resolve({scheduler:'busy', pending_chapters:3, pending_publish_jobs:2});
      else if (r.url === '/jobs/runs?limit=6') r.resolve([{id:'run1', cycle_number:77, status:'processed', outcomes:[]}]);
      else if (r.url === '/jobs?limit=6') r.resolve([{id:'j1', chapter_id:'Latest chapter', status:'publish_pending'}]);
      else r.resolve([]);
    });
    await tick();
    assert(content(tree).includes('运行轮次 #77'));
    assert(content(tree).includes('Latest chapter'));
    assert(content(tree).includes('summary unavailable'));
    nodes(tree).find(n => n.type === 'button' && content(n) === '重试').props.onClick();
    assert.equal(requests.length, 7);
    assert.equal(requests[6].url, '/jobs/runs/summary');
    requests[6].resolve({total_runs:1, processed_runs:1, idle_runs:0, total_processed_items:5});
    await tick();
    assert(!content(tree).includes('summary unavailable'));
    assert(content(tree).includes('运行轮次 #77'));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    path = Path(__file__).resolve().parents[1] / "app" / "static" / f"{page}-app.js"
    result = subprocess.run([node, "-e", script, page, str(path)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
