import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.application import create_fastapi_app
from app.services.trend_parsers import parse_fanqie_book_details, parse_provider_ranking
from app.services.trend_providers import fetch_hotlists

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "trends"


@pytest.mark.parametrize(
    ("provider", "fixture_name", "expected_title"),
    [
        ("fanqie", "fanqie-ranking.html", "惹金枝"),
        ("qimao", "qimao-ranking.html", "盖世神医"),
        ("qidian", "qidian-ranking.html", "捞尸人"),
    ],
)
def test_provider_parser_returns_normalized_ranked_items(
    provider: str,
    fixture_name: str,
    expected_title: str,
) -> None:
    html = (FIXTURE_DIR / fixture_name).read_text(encoding="utf-8")

    items = parse_provider_ranking(
        provider=provider,
        html=html,
        source_url="https://source.example/rank/",
    )

    assert len(items) == 2
    assert items[0]["rank"] == 1
    assert items[0]["title"] == expected_title
    assert items[0]["id"] == f"{provider}:1"
    assert set(items[0]) >= {
        "id",
        "provider",
        "rank",
        "title",
        "author",
        "category",
        "url",
        "raw_metadata",
    }
    assert items[1]["rank"] == 2


def test_provider_parsers_support_nested_text_relative_urls_and_missing_fields() -> None:
    fanqie_items = parse_provider_ranking(
        provider="fanqie",
        html=(FIXTURE_DIR / "fanqie-ranking.html").read_text(encoding="utf-8"),
        source_url="https://fanqienovel.com/rank",
    )
    qimao_items = parse_provider_ranking(
        provider="qimao",
        html=(FIXTURE_DIR / "qimao-ranking.html").read_text(encoding="utf-8"),
        source_url="https://www.qimao.com/paihang",
    )
    qidian_items = parse_provider_ranking(
        provider="qidian",
        html=(FIXTURE_DIR / "qidian-ranking.html").read_text(encoding="utf-8"),
        source_url="https://www.qidian.com/rank/",
    )

    assert fanqie_items[0]["title"] == "惹金枝"
    assert fanqie_items[0]["url"] == "https://fanqienovel.com/page/7231424472606051384"
    assert fanqie_items[0]["category"] is None
    assert qimao_items[0]["author"] == "狐颜乱语"
    assert qimao_items[0]["category"] == "都市"
    assert qidian_items[1]["author"] is None
    assert qidian_items[1]["category"] is None
    assert qidian_items[0]["raw_metadata"]["title_attributes"]["data-bid"] == "1041637443"


@pytest.mark.parametrize(
    ("provider", "fixture_name", "outside_noise"),
    [
        (
            "fanqie",
            "fanqie-ranking.html",
            '<div class="rank-book-item"><div class="book-item-index">99</div>'
            '<div class="title"><a href="/page/noise">榜外噪声</a></div></div>',
        ),
        (
            "qimao",
            "qimao-ranking.html",
            '<li class="rank-list-item"><span class="rank-number">99</span>'
            '<a class="s-book-title" href="/shuku/noise/">榜外噪声</a></li>',
        ),
    ],
)
def test_provider_parser_only_reads_items_inside_real_ranking_container(
    provider: str,
    fixture_name: str,
    outside_noise: str,
) -> None:
    html = outside_noise + (FIXTURE_DIR / fixture_name).read_text(encoding="utf-8")

    items = parse_provider_ranking(
        provider=provider,
        html=html,
        source_url="https://source.example/rank/",
    )

    assert [item["rank"] for item in items] == [1, 2]
    assert len({item["id"] for item in items}) == 2


def test_provider_parser_redacts_external_attributes_and_url_secrets() -> None:
    html = (FIXTURE_DIR / "qidian-ranking.html").read_text(encoding="utf-8")
    html = html.replace(
        'class="unfold" data-rid="1"',
        'class="unfold" data-rid="1" data-session-token="ITEM-SECRET"',
    ).replace(
        'class="book-name" data-bid="1041637443"',
        'class="book-name" data-bid="1041637443" '
        'data-auth-token="AUTH-SECRET" data-api-key="KEY-SECRET" '
        'data-signature="SIGNATURE-SECRET"',
    ).replace(
        '//www.qidian.com/book/1041637443/',
        '//www.qidian.com/book/1041637443/?token=URL-SECRET#session=FRAGMENT-SECRET',
        1,
    )

    item = parse_provider_ranking(
        provider="qidian",
        html=html,
        source_url="https://www.qidian.com/rank/",
    )[0]
    serialized = json.dumps(item, ensure_ascii=False)

    for secret in (
        "ITEM-SECRET",
        "AUTH-SECRET",
        "KEY-SECRET",
        "SIGNATURE-SECRET",
        "URL-SECRET",
        "FRAGMENT-SECRET",
    ):
        assert secret not in serialized
    assert item["url"] == "https://www.qidian.com/book/1041637443/"
    assert set(item["raw_metadata"]["item_attributes"]) <= {"class", "data-rid"}
    assert set(item["raw_metadata"]["title_attributes"]) <= {"class", "data-bid"}


def test_provider_parser_preserves_mixed_nested_text_order() -> None:
    html = """
    <div class="muye-rank-book-list">
      <div class="rank-book-item">
        <div class="book-item-index">01</div>
        <div class="title"><a href="/page/1">前<span>中</span>后</a></div>
      </div>
    </div>
    """

    item = parse_provider_ranking(
        provider="fanqie",
        html=html,
        source_url="https://fanqienovel.com/rank",
    )[0]

    assert item["title"] == "前中后"

@pytest.mark.parametrize("detail_mode", ["real", "mismatch", "obfuscated", "unavailable"])
def test_fanqie_collection_resolves_font_encoded_book_metadata(detail_mode) -> None:
    calls = []
    html = (FIXTURE_DIR / "fanqie-ranking.html").read_text(encoding="utf-8")
    html = html.replace("惹金枝", "惹\ue49c枝").replace("空留", "\ue473鹭\ue4e9双")

    def fake_get(url):
        calls.append(url)
        if "/page/" in url:
            page = {
                "bookId": "7231424472606051384" if detail_mode != "mismatch" else "999",
                "bookName": "惹金枝" if detail_mode != "obfuscated" else "惹\ue49c枝",
                "author": "白鹭成双",
            }
            text = "<script>window.__INITIAL_STATE__=" + json.dumps({"page": page}) + ";</script>"
            return httpx.Response(503 if detail_mode == "unavailable" else 200,
                                  request=httpx.Request("GET", url), text=text)
        return httpx.Response(200, request=httpx.Request("GET", url), text=html)

    provider = fetch_hotlists(http_get=fake_get)["providers"][0]
    if detail_mode == "real":
        assert provider["status"] == "real"
        assert provider["items"][0]["title"] == "惹金枝"
        assert provider["items"][0]["author"] == "白鹭成双"
        assert provider["items"][0]["id"] == "fanqie:1"
    else:
        assert provider["status"] == "degraded"
        assert provider["items"] == []
    assert len([url for url in calls if "/page/" in url]) == (2 if detail_mode == "real" else 1)


@pytest.mark.parametrize("category_v2", [
    [{"Name": "HE", "MainCategory": False}, {"Name": "古风世情", "MainCategory": True}],
    json.dumps([{"Name": "HE", "MainCategory": False}, {"Name": "古风世情", "MainCategory": True}]),
])
def test_fanqie_details_use_primary_category_instead_of_tags(category_v2):
    page = {"bookId": "1", "bookName": "惹金枝", "author": "空留", "categoryV2": category_v2}
    html = "<script>window.__INITIAL_STATE__=" + json.dumps({"page": page}) + ";</script>"
    assert parse_fanqie_book_details(html=html, book_id="1")["category"] == "古风世情"


def test_fanqie_fetches_categories_even_when_title_is_not_encoded():
    def fake_get(url):
        if "/page/" in url:
            page = {"bookId": url.rsplit("/", 1)[-1], "bookName": "正常书名", "author": "作者",
                    "categoryV2": json.dumps([{"Name": "古风世情", "MainCategory": True}])}
            html = "<script>window.__INITIAL_STATE__=" + json.dumps({"page": page}) + ";</script>"
        else:
            html = (FIXTURE_DIR / "fanqie-ranking.html").read_text(encoding="utf-8")
        return httpx.Response(200, request=httpx.Request("GET", url), text=html)

    provider = fetch_hotlists(http_get=fake_get)["providers"][0]
    assert provider["status"] == "real"
    assert [item["category"] for item in provider["items"]] == ["古风世情", "古风世情"]


def test_qidian_detailed_monthly_ranking_reads_each_books_category():
    html = '<div class="rank-list"><li data-rid="99"><a class="name">榜外书籍</a></li></div>'
    html += '<div class="rank-view-list"><div class="book-img-text"><ul>'
    for rank, title, category in [(1, "夜无疆", "玄幻"), (2, "玄鉴仙族", "仙侠")]:
        html += f'''<li data-rid="{rank}"><div class="book-mid-info">
          <h2><a href="//www.qidian.com/book/{rank}/" data-bid="{rank}">{title}</a></h2>
          <p class="author"><a class="name" href="//my.qidian.com/author/1/">作者{rank}</a>
          <em>|</em><a href="//www.qidian.com/xianxia">{category}</a><i>·</i>
          <a class="go-sub-type">次级分类</a></p></div></li>'''
    html += '</ul></div></div>'
    items = parse_provider_ranking(provider="qidian", html=html, source_url="https://www.qidian.com/rank/yuepiao/")
    assert [(i["title"], i["author"], i["category"]) for i in items] == [
        ("夜无疆", "作者1", "玄幻"), ("玄鉴仙族", "作者2", "仙侠")]


@pytest.mark.parametrize("browser_mode", ["real", "empty", "error"])
def test_qidian_collection_uses_browser_for_challenge_page(browser_mode) -> None:
    calls = []

    def fake_get(url):
        return httpx.Response(202, request=httpx.Request("GET", url),
                              text="<html><script src='/probe.js'></script></html>")

    def browser_get(url):
        calls.append(url)
        if browser_mode == "error":
            raise RuntimeError("private session details")
        if browser_mode == "empty":
            return "<html>验证页面</html>"
        return (FIXTURE_DIR / "qidian-ranking.html").read_text(encoding="utf-8")

    provider = fetch_hotlists(http_get=fake_get, browser_get=browser_get)["providers"][2]
    assert calls == ["https://www.qidian.com/rank/yuepiao/"]
    if browser_mode == "real":
        assert provider["status"] == "real"
        assert provider["items"][0]["title"] == "捞尸人"
    else:
        assert provider["status"] == "degraded"
        assert provider["items"] == []
        assert "private session details" not in provider["error"]


def test_qidian_skips_browser_when_http_ranking_is_available() -> None:
    def fake_get(url):
        return httpx.Response(200, request=httpx.Request("GET", url),
                              text=(FIXTURE_DIR / "qidian-ranking.html").read_text(encoding="utf-8"))

    def browser_get(url):
        pytest.fail("Valid rankings must not launch a browser")

    provider = fetch_hotlists(http_get=fake_get, browser_get=browser_get)["providers"][2]
    assert provider["status"] == "real"


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [("real", "real"), ("empty", "degraded"), ("http_error", "failed")],
)
def test_fetch_hotlists_uses_strict_collection_statuses(
    mode: str,
    expected_status: str,
) -> None:
    fixture_by_host = {
        "fanqienovel.com": "fanqie-ranking.html",
        "qimao.com": "qimao-ranking.html",
        "qidian.com": "qidian-ranking.html",
    }

    def fake_get(url: str) -> httpx.Response:
        request = httpx.Request("GET", url)
        if mode == "http_error":
            return httpx.Response(503, request=request, text="unavailable")
        if mode == "empty":
            return httpx.Response(200, request=request, text="<html><body>changed</body></html>")
        fixture_name = next(name for host, name in fixture_by_host.items() if host in url)
        return httpx.Response(
            200,
            request=request,
            text=(FIXTURE_DIR / fixture_name).read_text(encoding="utf-8"),
        )

    payload = fetch_hotlists(http_get=fake_get)

    assert [provider["status"] for provider in payload["providers"]] == [
        expected_status,
        expected_status,
        expected_status,
    ]
    for provider in payload["providers"]:
        assert provider["reference_samples"]
        if expected_status == "real":
            assert provider["items"]
            assert provider["items"] != provider["reference_samples"]
        else:
            assert provider["items"] == []


def test_hotlists_endpoint_returns_provider_grouped_entries() -> None:
    client = TestClient(create_fastapi_app())

    response = client.get("/trends/hotlists")

    assert response.status_code == 200
    payload = response.json()
    assert "providers" in payload
    assert isinstance(payload["providers"], list)
    assert "generated_at" in payload


def test_insights_endpoint_returns_keywords_and_categories() -> None:
    client = TestClient(create_fastapi_app())

    response = client.get("/trends/insights")

    assert response.status_code == 200
    payload = response.json()
    assert "top_keywords" in payload
    assert "top_categories" in payload
    assert "provider_counts" in payload
    assert "genre_buckets" in payload


def test_generate_direction_endpoint_uses_configured_generator() -> None:
    captured_payload = None

    def fake_direction_generator(payload: dict) -> dict:
        nonlocal captured_payload
        captured_payload = payload
        return {
            "direction_name": "都市逆袭成长",
            "target_platforms": ["番茄", "七猫"],
            "core_hook": "普通人逆势切入高压都市秩序",
            "audience": "男频快节奏读者",
            "title_candidates": ["逆流翻盘", "钢城起势"],
            "logline": "一个被压制的小人物，借热点行业震荡完成跃迁。",
            "selling_points": ["强钩子开局", "连续升级反馈"],
            "risk_flags": ["避免开局设定过重"],
            "outline": {
                "acts": ["入局", "受压", "破局"],
            },
            "opening_scene": {
                "location": "深夜工地",
                "conflict": "主角被迫接手烂摊子",
                "hook": "一次异常订单改变命运",
            },
            "chapter_plan": [
                {"chapter": 1, "goal": "主角受挫"},
                {"chapter": 2, "goal": "看到机会"},
            ],
        }

    app = create_fastapi_app()
    app.state.direction_generator = fake_direction_generator
    client = TestClient(app)

    response = client.post(
        "/trends/generate-direction",
        json={
            "direction_name": "都市逆袭成长",
            "selected_platforms": ["番茄", "七猫"],
            "keywords": ["都市", "逆袭", "成长"],
            "notes": "偏快节奏、强钩子、低门槛阅读",
        },
    )

    assert response.status_code == 200
    assert response.json()["direction_name"] == "都市逆袭成长"
    assert response.json()["title_candidates"][0] == "逆流翻盘"
    assert response.json()["selling_points"][0] == "强钩子开局"
    assert response.json()["opening_scene"]["location"] == "深夜工地"
    assert captured_payload == {
        "direction_name": "都市逆袭成长",
        "selected_platforms": ["番茄", "七猫"],
        "keywords": ["都市", "逆袭", "成长"],
        "notes": "偏快节奏、强钩子、低门槛阅读",
    }
    assert response.json()["source_trace"] is None


def _captured_hotlists_payload() -> dict:
    return {
        "generated_at": "2026-07-22T08:00:00+00:00",
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
                        "title": "都市逆袭",
                        "category": "都市",
                    }
                ],
                "reference_samples": [{"id": "sample:fanqie:1", "title": "仅供参考"}],
            },
            {
                "provider": "qimao",
                "provider_label": "七猫小说",
                "source_url": "https://www.qimao.com/paihang",
                "status": "degraded",
                "error": "no ranking items parsed",
                "items": [],
                "reference_samples": [{"id": "sample:qimao:1", "title": "仅供参考"}],
            },
        ],
    }


def test_capture_persists_one_fetch_and_serves_saved_insights(monkeypatch) -> None:
    calls = 0

    def fake_fetch_hotlists() -> dict:
        nonlocal calls
        calls += 1
        return _captured_hotlists_payload()

    monkeypatch.setattr("app.api.routes_trends.fetch_hotlists", fake_fetch_hotlists)
    client = TestClient(create_fastapi_app())

    capture_response = client.post("/trends/captures")

    assert capture_response.status_code == 201
    capture = capture_response.json()
    assert calls == 1
    assert len(capture["snapshots"]) == 2
    assert capture["snapshots"][0]["items"][0]["id"] == "fanqie:1"
    assert capture["snapshots"][1]["items"] == []
    assert "reference_samples" not in capture["snapshots"][0]
    assert capture["insights"]["provider_counts"]["番茄小说"] == 1

    def fail_if_refetched() -> dict:
        raise AssertionError("stored capture must not refetch providers")

    monkeypatch.setattr("app.api.routes_trends.fetch_hotlists", fail_if_refetched)
    insights_response = client.get(
        "/trends/insights",
        params={"capture_id": capture["capture_id"]},
    )
    list_response = client.get(
        "/trends/snapshots",
        params={"capture_id": capture["capture_id"]},
    )
    detail_response = client.get(f"/trends/snapshots/{capture['snapshots'][0]['id']}")

    assert insights_response.status_code == 200
    assert insights_response.json()["source_trace"]["capture_id"] == capture["capture_id"]
    assert len(list_response.json()) == 2
    assert detail_response.json()["capture_id"] == capture["capture_id"]
    assert client.get("/trends/insights?capture_id=missing").status_code == 404


def test_generate_direction_validates_and_forwards_capture_source_trace(monkeypatch) -> None:
    captured_payload = None

    def fake_direction_generator(payload: dict) -> dict:
        nonlocal captured_payload
        captured_payload = payload
        return {"direction_name": payload["direction_name"]}

    monkeypatch.setattr(
        "app.api.routes_trends.fetch_hotlists",
        _captured_hotlists_payload,
    )
    app = create_fastapi_app(direction_generator=fake_direction_generator)
    client = TestClient(app)
    capture = client.post("/trends/captures").json()

    response = client.post(
        "/trends/generate-direction",
        json={
            "direction_name": "都市逆袭成长",
            "selected_platforms": ["番茄"],
            "keywords": ["都市", "逆袭"],
            "capture_id": capture["capture_id"],
            "selected_item_ids": ["fanqie:1"],
            "notes": "强钩子",
        },
    )

    assert response.status_code == 200
    trace = response.json()["source_trace"]
    assert trace["capture_id"] == capture["capture_id"]
    assert trace["selected_item_ids"] == ["fanqie:1"]
    assert set(trace["snapshot_ids"]) == {item["id"] for item in capture["snapshots"]}
    assert captured_payload is not None
    assert captured_payload["source_trace"] == trace

    assert client.post(
        "/trends/generate-direction",
        json={
            "direction_name": "invalid",
            "selected_platforms": ["七猫"],
            "keywords": [],
            "capture_id": capture["capture_id"],
            "selected_item_ids": ["fanqie:1"],
        },
    ).status_code == 400


def test_generate_direction_rejects_non_object_generator_result() -> None:
    app = create_fastapi_app(direction_generator=lambda payload: [])
    client = TestClient(app)

    response = client.post(
        "/trends/generate-direction",
        json={
            "direction_name": "都市逆袭成长",
            "selected_platforms": ["番茄"],
            "keywords": ["都市"],
        },
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "direction generator returned invalid result"


def test_generate_direction_preserves_validated_trace_when_generator_mutates_input(
    monkeypatch,
) -> None:
    def mutating_direction_generator(payload: dict) -> dict:
        payload["source_trace"]["capture_id"] = "forged_capture"
        payload["source_trace"]["snapshot_ids"].clear()
        payload["source_trace"]["selected_item_ids"].append("forged:item")
        return {
            "direction_name": payload["direction_name"],
            "source_trace": {"capture_id": "forged_result"},
        }

    monkeypatch.setattr(
        "app.api.routes_trends.fetch_hotlists",
        _captured_hotlists_payload,
    )
    app = create_fastapi_app(direction_generator=mutating_direction_generator)
    client = TestClient(app)
    capture = client.post("/trends/captures").json()

    response = client.post(
        "/trends/generate-direction",
        json={
            "direction_name": "都市逆袭成长",
            "selected_platforms": ["番茄"],
            "keywords": ["都市"],
            "capture_id": capture["capture_id"],
            "selected_item_ids": ["fanqie:1"],
        },
    )

    assert response.status_code == 200
    assert response.json()["source_trace"] == {
        "capture_id": capture["capture_id"],
        "snapshot_ids": [item["id"] for item in capture["snapshots"]],
        "providers": [item["provider"] for item in capture["snapshots"]],
        "selected_item_ids": ["fanqie:1"],
        "captured_at": capture["snapshots"][0]["captured_at"],
    }
    assert client.post(
        "/trends/generate-direction",
        json={
            "direction_name": "invalid",
            "selected_platforms": ["番茄"],
            "keywords": [],
            "selected_item_ids": ["fanqie:1"],
        },
    ).status_code == 400
