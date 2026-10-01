"""Serving the staff web app from the API's own origin.

The catch-all route is the last thing in api.py and answers every GET nothing
else claimed. That makes two failure modes worth pinning down:

  * it must not shadow the API — a mistyped /api path has to be a JSON 404, not
    an HTML page a client then chokes on;
  * it must not read outside its own directory — "../.env" is one request away
    from handing a stranger the database password.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import api  # noqa: E402


@pytest.fixture()
def webapp(tmp_path, monkeypatch):
    d = tmp_path / "webapp"
    (d / "_expo" / "static" / "js" / "web").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>Exceed Box</title>APP-INDEX")
    (d / "_expo" / "static" / "js" / "web" / "index-abc123.js").write_text("console.log(1)")
    (d / "favicon.ico").write_bytes(b"ICO")
    # a secret sitting next to the build directory, as .env does on the server
    (tmp_path / ".env").write_text("DATABASE_URL=postgres://secret")
    monkeypatch.setattr(api, "WEBAPP_DIR", d)
    return d


client = TestClient(api.app)


def test_root_serves_the_app(webapp):
    r = client.get("/")
    assert r.status_code == 200 and "APP-INDEX" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_client_side_routes_fall_back_to_the_app(webapp):
    for path in ("/today", "/leads/42", "/pipeline", "/admin/users"):
        r = client.get(path)
        assert r.status_code == 200 and "APP-INDEX" in r.text, path


def test_hashed_bundles_are_cached_for_a_year(webapp):
    r = client.get("/_expo/static/js/web/index-abc123.js")
    assert r.status_code == 200 and "console.log" in r.text
    assert "immutable" in r.headers["cache-control"]


def test_plain_static_files_are_served(webapp):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.content == b"ICO"


@pytest.mark.parametrize("attack", [
    "/../.env", "/..%2F.env", "/%2e%2e/.env", "/_expo/../../.env",
    "/_expo/static/../../../.env",
])
def test_traversal_never_reads_outside_the_build(webapp, attack):
    r = client.get(attack)
    assert "DATABASE_URL" not in r.text
    assert "secret" not in r.text


def test_a_mistyped_api_path_is_a_json_404_not_the_app(webapp):
    r = client.get("/api/this-does-not-exist")
    assert r.status_code == 404
    assert "APP-INDEX" not in r.text
    assert r.headers["content-type"].startswith("application/json")


def test_real_routes_still_win_over_the_catch_all(webapp):
    assert "APP-INDEX" not in client.get("/api/health").text
    assert "APP-INDEX" not in client.get("/book").text
    assert "APP-INDEX" not in client.get("/_status").text


def test_missing_build_is_an_honest_503(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "WEBAPP_DIR", tmp_path / "nothing-here")
    r = client.get("/")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "webapp_not_deployed"
