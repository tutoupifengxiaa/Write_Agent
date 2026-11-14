from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_chapter_gate_endpoint_rejects_missing_chapter():
    with TestClient(app) as client:
        response = client.post(
            "/chapters/chapter_123/gate",
            json={"action": "approve"},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "chapter not found"
