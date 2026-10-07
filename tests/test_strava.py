from unittest.mock import Mock

import pytest

from hevy2garmin import strava


class Store:
    def __init__(self):
        self.values = {}

    def get_app_config(self, key):
        return self.values.get(key)

    def set_app_config(self, key, value):
        self.values[key] = value


def client_for(description="My personal notes", name="Afternoon Weight Training"):
    client = Mock()
    client.find.return_value = {"id": 123, "name": name, "description": description}
    client.request.side_effect = lambda method, path, **kw: {"id": 123, **kw["json"]}
    return client


def test_enrichment_preserves_notes_metrics_and_is_idempotent(sample_workout):
    store = Store()
    client = client_for()
    assert strava.enrich(store, sample_workout, "456", client) == "updated"
    method, path = client.request.call_args.args
    body = client.request.call_args.kwargs["json"]
    assert (method, path) == ("PUT", "/activities/123")
    assert set(body) == {"name", "description"}
    assert body["name"] == "Push"
    assert body["description"].startswith("My personal notes\n\n")
    assert "Bench Press" in body["description"]
    assert store.values["strava_workout_test-workout-123_backup"]["description"] == "My personal notes"
    assert strava.enrich(store, sample_workout, "456", client) == "unchanged"
    assert client.request.call_count == 1


def test_retry_when_strava_has_not_received_garmin_activity(sample_workout):
    client = Mock()
    client.find.return_value = None
    store = Store()
    assert strava.enrich(store, sample_workout, "456", client) == "pending"
    assert not store.values
    client.request.assert_not_called()


def test_edited_workout_replaces_block_and_preserves_custom_title(sample_workout):
    store = Store()
    first = client_for()
    strava.enrich(store, sample_workout, "456", first)
    old = first.request.call_args.kwargs["json"]["description"]
    second = client_for(old + "\n\nNotes added later", "My custom title")
    sample_workout["title"] = "Push revised"
    strava.enrich(store, sample_workout, "456", second)
    body = second.request.call_args.kwargs["json"]
    assert "name" not in body
    assert body["description"].count(strava.BEGIN) == 1
    assert "My personal notes" in body["description"]
    assert "Notes added later" in body["description"]
    assert "Push revised" in body["description"]


@pytest.mark.parametrize("external_ids", [["garmin_push_456"], ["garmin_push_999"], ["garmin_push_456", "garmin_push_456"]])
def test_matching_requires_unique_exact_garmin_id(sample_workout, external_ids):
    client = strava.StravaClient(Store())
    rows = [{"id": n, "external_id": eid} for n, eid in enumerate(external_ids)]
    client.request = Mock(side_effect=[rows, {"id": 0, "external_id": "garmin_push_456"}])
    match = client.find(sample_workout, "456")
    assert bool(match) == (external_ids == ["garmin_push_456"])


def test_failed_update_does_not_mark_synced(sample_workout):
    client = client_for()
    client.request.side_effect = RuntimeError("rate limit")
    store = Store()
    with pytest.raises(RuntimeError):
        strava.enrich(store, sample_workout, "456", client)
    assert "strava_workout_test-workout-123" not in store.values


def test_refresh_tokens_are_saved_before_api_call(monkeypatch):
    store = Store()
    store.values[strava.TOKEN_KEY] = {"access_token": "expired", "refresh_token": "refresh", "expires_at": 0}
    response = Mock(status_code=200)
    response.json.return_value = {"access_token": "fresh", "refresh_token": "rotated", "expires_at": 9999999999}
    post = Mock(return_value=response)
    request = Mock(return_value=Mock(ok=True, json=lambda: []))
    monkeypatch.setattr(strava.requests, "post", post)
    monkeypatch.setattr(strava.requests, "request", request)
    strava.StravaClient(store).request("GET", "/athlete/activities")
    assert store.values[strava.TOKEN_KEY]["refresh_token"] == "rotated"
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh"


def test_existing_merges_are_backfilled_without_resyncing_garmin(monkeypatch, sample_workout):
    monkeypatch.setattr(strava, "configured", lambda: True)
    store = Store()
    store.values[strava.TOKEN_KEY] = {"access_token": "token"}
    store.get_recent_synced = Mock(return_value=[{"hevy_id": sample_workout["id"], "garmin_activity_id": "456", "sync_method": "merge"}])
    hevy = Mock()
    hevy.get_workout.return_value = sample_workout
    enrich = Mock(return_value="updated")
    monkeypatch.setattr(strava, "enrich", enrich)
    assert strava.sync_recent(store, hevy)["updated"] == 1
    enrich.assert_called_once_with(store, sample_workout, "456")


def test_oauth_callback_rejects_forged_or_expired_state(monkeypatch):
    from fastapi.testclient import TestClient
    from hevy2garmin import server
    monkeypatch.setattr(server, "_is_configured_cache", True)
    monkeypatch.setattr(server.db, "get_db", lambda: Store())
    client = TestClient(server.app)
    client.cookies.set("strava_state", "expected")
    assert client.get("/strava/callback?state=forged&code=x").status_code == 400
    assert client.get("/strava/callback?state=expected&code=x").status_code == 400


def test_oauth_callback_accepts_only_once_and_requires_scopes(monkeypatch):
    import hashlib
    import time
    from fastapi.testclient import TestClient
    from hevy2garmin import server
    store = Store()
    state = "test-state"
    key = "strava_state_" + hashlib.sha256(state.encode()).hexdigest()
    store.values[key] = {"expires": time.time() + 600}
    monkeypatch.setattr(server, "_is_configured_cache", True)
    monkeypatch.setattr(server.db, "get_db", lambda: store)
    exchange = Mock()
    monkeypatch.setattr(strava, "exchange_token", exchange)
    client = TestClient(server.app)
    client.cookies.set("strava_state", state)
    query = "/strava/callback?state=test-state&code=x&scope=activity:read_all,activity:write"
    assert client.get(query, follow_redirects=False).status_code == 303
    exchange.assert_called_once()
    client.cookies.set("strava_state", state)
    assert client.get(query, follow_redirects=False).status_code == 400
