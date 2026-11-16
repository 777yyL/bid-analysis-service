from contextlib import closing
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from analyzer.app import create_app
from analyzer.config import Settings
from analyzer.sales_models import RuntimeSettings
from analyzer.sales_sync import parse_time, source_conninfo, sync_signature, time_expression
from analyzer.source import SourceError


def test_time_selection_uses_ingestion_not_publish_date_and_keeps_timezone():
    columns = {
        "updated_at": "timestamp without time zone",
        "first_seen_at": "timestamp without time zone",
        "publish_at": "timestamp without time zone",
    }
    expr, names = time_expression(
        columns, "n", "", ("updated_at", "first_seen_at"), "Asia/Shanghai"
    )
    assert names == ["updated_at", "first_seen_at"] and "publish_at" not in expr
    assert "AT TIME ZONE 'Asia/Shanghai'" in expr
    assert parse_time("2026-11-01T08:00:00") == datetime(2026, 11, 1, tzinfo=timezone.utc)
    with pytest.raises(SourceError, match="timestamp"):
        time_expression(columns, "n", "nonexistent", (), "Asia/Shanghai")


def test_signature_tracks_scope_and_clock_but_not_batch_size_or_model():
    cfg = RuntimeSettings(source_dsn="postgresql://readonly:secret@main/source").model_dump()
    assert sync_signature(cfg) == sync_signature(
        {**cfg, "source_sync_batch_hours": 12, "embedding_model": "other"}
    )
    for key, value in [
        ("collect_attachments", False),
        ("allow_later_stages", True),
        ("source_timezone", "UTC"),
    ]:
        assert sync_signature(cfg) != sync_signature({**cfg, key: value})
    assert "default_transaction_read_only=on" in source_conninfo(cfg)
    assert "direct-sync" in source_conninfo(cfg)


def test_source_api_auth_and_local_batch_records(tmp_path):
    cfg = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        worker_enabled=False,
        analyzer_api_key="test",
        sales_secret_key_file=str(tmp_path / "key"),
    )
    app = create_app(cfg)
    app.state.store.initialize(sales_only=True)
    with closing(TestClient(app)) as client:
        assert client.get("/api/v3/source/sync/batches").status_code == 401
        headers = {"X-API-Key": "test"}
        assert client.get("/api/v3/source/sync/batches", headers=headers).json() == {
            "total": 0,
            "items": [],
        }
        assert (
            client.get("/api/v3/source/sync/batches/unknown/records", headers=headers).status_code
            == 404
        )
        status = client.get("/api/v3/source/sync/status", headers=headers).json()
        assert status["ready"] is False and status["storage"] == "tables"
    app.state.store.close()


@pytest.mark.asyncio
async def test_queued_analyses_preserved_until_local_data_is_ready(tmp_path):
    cfg = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'pending.db'}",
        worker_enabled=False,
        sales_secret_key_file=str(tmp_path / "key"),
    )
    app = create_app(cfg)
    service = app.state.sales
    service.store.initialize()
    service.save_settings({"engine_enabled": True})
    service.enqueue("analyze", {"project_key": "p"})
    assert await service.run_once() is False
    assert service.jobs()["items"][0]["status"] == "queued"
    service.store.close()
