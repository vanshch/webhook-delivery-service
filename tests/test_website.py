"""Integration checks for the public project face and its narrow file boundary."""

import pytest


def test_project_page_and_head_are_public_without_redis(client, monkeypatch):
    def fail_connection():
        raise AssertionError("The landing page must not depend on Redis")

    monkeypatch.setattr("app.storage.redis_client.get_async_redis", fail_connection)
    response = client.get("/")
    assert response.status_code == 200
    assert "Every event" in response.text
    assert 'href="/docs"' in response.text
    assert "Sample events; no production traffic." in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    head = client.head("/")
    assert head.status_code == 200
    assert not head.content


@pytest.mark.parametrize("filename,media", [
    ("styles.css", "text/css"), ("app.mjs", "javascript"),
    ("scenarios.mjs", "javascript"), ("mark.svg", "image/svg+xml"),
])
def test_public_assets_have_browser_usable_types(client, filename, media):
    response = client.get(f"/assets/web/{filename}")
    assert response.status_code == 200
    assert media in response.headers["content-type"]


@pytest.mark.parametrize("path", [
    "/assets/web/.env", "/assets/web/context.md", "/assets/web/../index.html",
    "/assets/web/%2e%2e/%2e%2e/%2e%2e/.env", "/evidence/.env",
    "/evidence/unknown", "/tools/frontend-ab/private/auth.json",
    "/docs/evidence/oracle-postfix-20260906T213502Z.json",
])
def test_only_public_assets_and_named_evidence_are_served(client, path):
    assert client.get(path).status_code == 404


def test_historical_evidence_is_available_and_api_contracts_remain(client):
    soak = client.get("/evidence/soak")
    benchmark = client.get("/evidence/benchmark")
    assert soak.status_code == benchmark.status_code == 200
    assert isinstance(soak.json(), dict)
    assert isinstance(benchmark.json(), dict)
    schema = client.get("/openapi.json").json()
    assert "/webhooks" in schema["paths"]
    assert "/deliveries/{event_id}" in schema["paths"]
    assert "/" not in schema["paths"]
    assert client.get("/docs").status_code == 200
