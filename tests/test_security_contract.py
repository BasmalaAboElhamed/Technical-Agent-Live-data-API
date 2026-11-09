from __future__ import annotations

import json
from pathlib import Path
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app


def test_api_key_auth_enforced_when_configured(settings, repo):
    auth_settings = settings.__class__(**{**settings.__dict__, "api_keys": ("my-super-secret-key",)})
    app = create_app(auth_settings)
    with TestClient(app) as client:
        # 1. /health is public
        res_health = client.get("/health")
        assert res_health.status_code == 200

        # 2. Protected endpoint without key -> 401
        res_no_key = client.get("/api/v1/stocks")
        assert res_no_key.status_code == 401
        assert res_no_key.json()["error"]["code"] == "unauthorized"

        # 3. Protected endpoint with wrong key -> 401
        res_bad_key = client.get("/api/v1/stocks", headers={"X-API-Key": "wrong-key"})
        assert res_bad_key.status_code == 401
        assert res_bad_key.json()["error"]["code"] == "unauthorized"

        # 4. Protected endpoint with valid key -> 200
        res_good_key = client.get("/api/v1/stocks", headers={"X-API-Key": "my-super-secret-key"})
        assert res_good_key.status_code == 200


def test_rate_limiting_per_identity(settings, repo):
    rl_settings = settings.__class__(**{
        **settings.__dict__,
        "api_keys": ("key-rate-limited",),
        "rate_limit_per_minute": 3,
    })
    app = create_app(rl_settings)
    with TestClient(app) as client:
        headers = {"X-API-Key": "key-rate-limited"}
        for _ in range(3):
            r = client.get("/api/v1/stocks", headers=headers)
            assert r.status_code == 200

        # 4th request exceeds limit of 3
        blocked = client.get("/api/v1/stocks", headers=headers)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"
        assert "Retry-After" in blocked.headers
        assert int(blocked.headers["Retry-After"]) >= 1


def test_request_body_size_limit(settings):
    size_settings = settings.__class__(**{**settings.__dict__, "max_request_body_bytes": 50})
    app = create_app(size_settings)
    with TestClient(app) as client:
        # Request with oversized body
        large_body = "x" * 100
        res = client.get("/health", headers={"Content-Length": str(len(large_body))})
        assert res.status_code == 413
        assert res.json()["error"]["code"] == "payload_too_large"


def test_no_db_path_exposure_in_errors(settings, repo):
    app = create_app(settings)
    with TestClient(app) as client:
        # Trigger not found
        res = client.get("/api/v1/stocks/DOES_NOT_EXIST_XYZ")
        assert res.status_code == 404
        text = res.text
        assert "sqlite" not in text.lower()
        assert "data/egx" not in text.lower()
        assert "SELECT" not in text


def test_frozen_openapi_contract_matches_live_schema(settings):
    frozen_path = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"
    assert frozen_path.exists(), "docs/openapi.json must exist"
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))

    app = create_app(settings)
    live = app.openapi()

    assert set(live["paths"].keys()) == set(frozen["paths"].keys())
    for path, methods in frozen["paths"].items():
        for m in methods:
            assert m in live["paths"][path], f"Method {m} missing from live path {path}"
