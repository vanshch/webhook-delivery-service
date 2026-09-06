"""Tests for structured request-ID handling."""


def test_request_id_is_returned(client):
    response = client.get("/livez", headers={"x-request-id": "request-123"})

    assert response.headers["x-request-id"] == "request-123"


def test_unsafe_request_id_is_replaced(client):
    response = client.get("/livez", headers={"x-request-id": "unsafe value"})

    request_id = response.headers["x-request-id"]
    assert request_id.startswith("req_")
    assert request_id != "unsafe value"
