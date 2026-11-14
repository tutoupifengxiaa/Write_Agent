from __future__ import annotations

from collections import Counter

GENRE_BUCKET_RULES = {
    "都市": "都市现实",
    "现言": "情感关系",
    "历史": "历史权谋",
    "高武": "升级战斗",
    "玄幻": "升级战斗",
    "仙侠": "升级战斗",
}


def build_trend_insights(hotlists_payload: dict) -> dict:
    keyword_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()
    provider_counts: Counter[str] = Counter()
    genre_buckets: Counter[str] = Counter()

    for provider in hotlists_payload.get("providers", []):
        label = provider.get("provider_label", provider.get("provider", "unknown"))
        items = provider.get("items", [])
        provider_counts[label] += len(items)
        for item in items:
            category = item.get("category") or "未分类"
            category_counter[category] += 1
            genre_buckets[_map_genre_bucket(category)] += 1
            title = item.get("title", "")
            for keyword in _extract_keywords(title):
                keyword_counter[keyword] += 1

    return {
        "top_keywords": [
            {"keyword": keyword, "count": count}
            for keyword, count in keyword_counter.most_common(8)
        ],
        "top_categories": [
            {"category": category, "count": count}
            for category, count in category_counter.most_common(6)
        ],
        "provider_counts": dict(provider_counts),
        "genre_buckets": [
            {"bucket": bucket, "count": count}
            for bucket, count in genre_buckets.most_common(6)
        ],
    }


def _extract_keywords(title: str) -> list[str]:
    normalized = (
        title.replace("：", " ")
        .replace(":", " ")
        .replace("，", " ")
        .replace(",", " ")
        .replace("·", " ")
    )
    words = [part.strip() for part in normalized.split() if part.strip()]
    keywords: list[str] = []
    for word in words:
        if len(word) >= 2:
            keywords.append(word[:6])
    return keywords[:3]


def _map_genre_bucket(category: str) -> str:
    return GENRE_BUCKET_RULES.get(category, "其他趋势")
