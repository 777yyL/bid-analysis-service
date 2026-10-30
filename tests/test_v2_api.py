import time

from fastapi.testclient import TestClient

from analyzer.app import create_app
from analyzer.config import Settings


def test_demo_tags_review_and_queue_controls(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'api.db'}",
                   analyzer_api_key="test", worker_poll_seconds=0.01)
    headers = {"X-API-Key": "test"}
    with TestClient(create_app(cfg)) as client:
        assert client.get("/api/v2/overview").status_code == 401
        assert client.post("/api/v2/demo", headers=headers).status_code == 200
        for _ in range(100):
            notices = client.get("/api/v2/notices", headers=headers).json()
            if any(n["tags"] for n in notices["items"]):
                break
            time.sleep(0.02)
        assert notices["total"] == 3
        product = next(n for n in notices["items"] if any(t["dimension"] == "product" for t in n["tags"]))
        tag = next(t for t in product["tags"] if t["dimension"] == "product")
        body = {**{k: tag[k] for k in ("dimension", "category", "label")},
                "status": "excluded", "reason": "人工复核"}
        assert client.post(f'/api/v2/notices/{product["notice_id"]}/review', headers=headers,
                           json=body).status_code == 200
        detail = client.get(f'/api/v2/notices/{product["notice_id"]}', headers=headers).json()
        assert detail["manual_tags"][0]["status"] == "excluded"
        assert client.post("/api/v2/tasks/control", headers=headers,
                           json={"paused": True, "llm_rpm": 6}).json()["paused"]
        assert client.get("/api/v2/tasks", headers=headers).json()["scheduler"]["llm_rpm"] == 6
        assert client.get("/api/v2/projects", headers=headers).json()["total"] > 0
        assert client.get("/api/v2/notices/export.csv", headers=headers).status_code == 200


def test_product_lexicon_survives_business_sync(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'api.db'}",
                   analyzer_api_key="test", worker_enabled=False)
    with TestClient(create_app(cfg)) as client:
        h = {"X-API-Key": "test"}
        client.post("/api/v2/demo", headers=h)
        knowledge = client.get("/api/v2/lexicon", headers=h).json()
        assert knowledge["product_keywords"]
        knowledge["version"] = "edited"
        assert client.put("/api/v2/lexicon", headers=h, json=knowledge).status_code == 200
        trial = client.post("/api/v2/lexicon/trial", headers=h,
                            json={"title": "交换机采购", "body": "采购交换机10台"}).json()
        assert any(t["dimension"] == "product" for t in trial["tags"])


def test_history_sync_exposes_pause_and_persists_requested_batch_size(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'sync-api.db'}",
        analyzer_api_key="test", source_db_dsn="postgresql://readonly@db/bidinfo", source_file_root="/source-data")
    with TestClient(create_app(cfg)) as client:
        h = {"X-API-Key": "test"}
        client.post("/api/v2/demo", headers=h)
        client.post("/api/v2/tasks/control", headers=h, json={"paused": True})
        response = client.post("/api/v2/sync", headers=h, json={"mode": "history", "page_size": 7})
        assert response.status_code == 200
        assert response.json()["paused"] is True
        stream = client.get("/api/v2/overview", headers=h).json()["sync"]["history"]
        assert stream["page_size"] == 7
        assert stream["phase"] == "paused"


def test_removed_keyword_does_not_trap_manual_label(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'api.db'}",
                   analyzer_api_key="test", worker_enabled=False)
    with TestClient(create_app(cfg)) as client:
        h = {"X-API-Key": "test"}
        client.post("/api/v2/demo", headers=h)
        tag = {"dimension": "product", "category": "网络产品", "label": "交换机",
               "status": "confirmed", "reason": "人工确认"}
        assert client.post("/api/v2/notices/900000001/review", headers=h, json=tag).status_code == 200
        kb = client.get("/api/v2/lexicon", headers=h).json()
        kb["product_keywords"] = []
        client.put("/api/v2/lexicon", headers=h, json=kb)
        tag.update(status="excluded", reason="已停用的标签")
        assert client.post("/api/v2/notices/900000001/review", headers=h, json=tag).status_code == 200
