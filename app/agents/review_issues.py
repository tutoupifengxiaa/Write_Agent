from __future__ import annotations

from hashlib import sha256


ALLOWED_TYPES = {"coverage", "constraint", "continuity", "logic", "pacing", "safety", "style"}
ALLOWED_SEVERITIES = {"low", "medium", "high"}
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


def issue_fingerprint(issue_type: str, message: str) -> str:
    normalized_message = " ".join(message.casefold().split())
    digest = sha256(f"{issue_type}\x1f{normalized_message}".encode("utf-8")).hexdigest()[:12]
    return f"issue_{digest}"


def normalize_review_issues(raw_issues: list[dict] | None) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    ordered_keys: list[tuple[str, str]] = []

    for raw in raw_issues or []:
        message = str(raw.get("message") or "").strip()
        if not message:
            continue

        issue_type = str(raw.get("type") or "style").strip().lower()
        if issue_type not in ALLOWED_TYPES:
            issue_type = "style"

        severity = str(raw.get("severity") or "medium").strip().lower()
        if severity not in ALLOWED_SEVERITIES:
            severity = "medium"

        key = (issue_type, " ".join(message.casefold().split()))
        candidate = {
            "id": issue_fingerprint(issue_type, message),
            "type": issue_type,
            "severity": severity,
            "message": message,
            "evidence": str(raw.get("evidence") or "").strip(),
            "source": "rule" if raw.get("source") == "rule" else "model",
        }
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            ordered_keys.append(key)
            continue

        if SEVERITY_RANK[candidate["severity"]] > SEVERITY_RANK[existing["severity"]]:
            existing["severity"] = candidate["severity"]
        if len(candidate["evidence"]) > len(existing["evidence"]):
            existing["evidence"] = candidate["evidence"]
        if candidate["source"] == "rule":
            existing["source"] = "rule"

    return [merged[key] for key in ordered_keys]


def has_blocking_issues(issues: list[dict]) -> bool:
    return any(issue["severity"] in {"medium", "high"} for issue in issues)
