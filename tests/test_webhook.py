"""Tests for the /api/cron/webhook receiver + staged retry worker."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_cron_secret():
    with patch.dict(os.environ, {"CRON_SECRET": "cron-123"}):
        os.environ.pop("VERCEL", None)
        from hevy2garmin.server import app
        yield TestClient(app)


class TestWebhookEndpoint:
    def test_rejects_missing_bearer(self, client_with_cron_secret) -> None:
        resp = client_with_cron_secret.post("/api/cron/webhook")
        assert resp.status_code == 401

    def test_rejects_wrong_bearer(self, client_with_cron_secret) -> None:
        resp = client_with_cron_secret.post(
            "/api/cron/webhook", headers={"Authorization": "Bearer nope"}
        )
        assert resp.status_code == 401

    def test_accepts_and_schedules_background_sync(self, client_with_cron_secret) -> None:
        """Valid Bearer → immediate 200 (Hevy requires an answer within 5 s)."""
        with patch("hevy2garmin.server._webhook_sync", new_callable=AsyncMock) as worker:
            resp = client_with_cron_secret.post(
                "/api/cron/webhook", headers={"Authorization": "Bearer cron-123"}
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "accepted"}
        worker.assert_called_once()

    def test_not_blocked_by_dashboard_auth(self) -> None:
        """POST /api/cron/webhook bypasses the cookie/X-Api-Key middleware."""
        with patch.dict(
            os.environ, {"HEVY2GARMIN_SECRET": "dash-secret", "CRON_SECRET": "cron-123"}
        ):
            os.environ.pop("VERCEL", None)
            from hevy2garmin.server import app
            client = TestClient(app)
            with patch("hevy2garmin.server._webhook_sync", new_callable=AsyncMock):
                resp = client.post(
                    "/api/cron/webhook", headers={"Authorization": "Bearer cron-123"}
                )
        assert resp.status_code == 200


class TestWebhookAuthFailsClosed:
    """No CRON_SECRET must mean unavailable, not unauthenticated.

    /api/cron/webhook is internet-facing (Hevy calls it) and is exempt from the
    dashboard cookie/CSRF middleware, so an `if secret:` guard that skips the
    check when the secret is unset leaves an anonymous sync trigger exposed —
    including on an instance whose owner did set a dashboard password.
    """

    def test_unset_cron_secret_refuses_instead_of_accepting(self) -> None:
        with patch.dict(os.environ, {"HEVY2GARMIN_SECRET": "dash-password"}):
            os.environ.pop("CRON_SECRET", None)
            os.environ.pop("VERCEL", None)
            from hevy2garmin.server import app

            with patch("hevy2garmin.server._webhook_sync", new_callable=AsyncMock) as worker:
                resp = TestClient(app).post("/api/cron/webhook")
            assert resp.status_code == 503
            assert "CRON_SECRET" in resp.json()["error"]
            worker.assert_not_called(), "no sync may be scheduled by an unauthenticated caller"

    def test_a_near_miss_token_is_rejected(self, client_with_cron_secret) -> None:
        for bad in ("Bearer cron-12", "Bearer cron-1234", "cron-123", "Basic cron-123", ""):
            resp = client_with_cron_secret.post(
                "/api/cron/webhook", headers={"Authorization": bad}
            )
            assert resp.status_code == 401, bad

    def test_correct_token_still_accepted(self, client_with_cron_secret) -> None:
        with patch("hevy2garmin.server._webhook_sync", new_callable=AsyncMock):
            resp = client_with_cron_secret.post(
                "/api/cron/webhook", headers={"Authorization": "Bearer cron-123"}
            )
        assert resp.status_code == 200

    def test_bearer_check_is_constant_time(self) -> None:
        """A `!=` compare on the raw string leaks the secret byte by byte."""
        import inspect

        from hevy2garmin import server

        src = inspect.getsource(server._bearer_ok)
        assert "compare_digest" in src


class TestInFlightCap:
    """A staged sync lives ~25 min, so unbounded spawning is a pile-up vector."""

    def test_beyond_the_cap_no_new_task_is_spawned(self, client_with_cron_secret) -> None:
        from hevy2garmin import server

        filler = {object() for _ in range(server.WEBHOOK_MAX_INFLIGHT)}
        with (
            patch.object(server, "_webhook_tasks", filler),
            patch("hevy2garmin.server._webhook_sync", new_callable=AsyncMock) as worker,
        ):
            resp = client_with_cron_secret.post(
                "/api/cron/webhook", headers={"Authorization": "Bearer cron-123"}
            )
        assert resp.status_code == 200, "Hevy must not be told to retry into the same wall"
        assert resp.json()["status"] == "throttled"
        worker.assert_not_called()

    def test_under_the_cap_still_schedules(self, client_with_cron_secret) -> None:
        from hevy2garmin import server

        with (
            patch.object(server, "_webhook_tasks", set()),
            patch("hevy2garmin.server._webhook_sync", new_callable=AsyncMock) as worker,
        ):
            resp = client_with_cron_secret.post(
                "/api/cron/webhook", headers={"Authorization": "Bearer cron-123"}
            )
        assert resp.json()["status"] == "accepted"
        worker.assert_called_once()


class TestWebhookWorker:
    """Every automatic retry must be merge-only, including the final one."""

    def _run(self, responses: list[dict]) -> list[bool]:
        from hevy2garmin import server

        calls: list[bool] = []

        async def fake_sync_one(merge_only=False, **kwargs):
            calls.append(merge_only)
            return JSONResponse(responses[len(calls) - 1])

        with (
            patch.object(server, "WEBHOOK_DELAY_SECONDS", 0),
            patch.object(server, "WEBHOOK_RETRY_INTERVAL_SECONDS", 0),
            patch.object(server, "_sync_one_recorded", fake_sync_one),
        ):
            asyncio.run(server._webhook_sync())
        return calls

    def test_merge_only_on_every_attempt(self) -> None:
        pending = {"synced": 0, "merge_pending": True, "done": False}
        calls = self._run([pending, pending, pending])
        assert calls == [True, True, True]

    def test_stops_after_first_successful_sync(self) -> None:
        calls = self._run([{"synced": 1, "done": True}])
        assert calls == [True]

    def test_stops_when_nothing_is_pending(self) -> None:
        calls = self._run([{"synced": 0, "merge_pending": False, "done": False}])
        assert calls == [True]

    def test_a_lock_collision_is_retried_not_treated_as_done(self) -> None:
        """auto-sync holding the lock is not an answer about this workout.

        The busy reply carries no merge_pending, so a plain "did it merge?"
        check reads it as "nothing to do" and abandons the webhook sync — the
        workout then waits for the next auto-sync, which is the delay the
        webhook exists to remove.
        """
        busy = {"error": "Sync already running", "busy": True}
        calls = self._run([busy, busy, {"synced": 1, "done": True}])
        assert calls == [True, True, True]

    def test_a_raising_attempt_stops_the_worker(self) -> None:
        from hevy2garmin import server

        calls: list[bool] = []

        async def boom(merge_only=False, **kwargs):
            calls.append(merge_only)
            raise RuntimeError("Garmin unreachable")

        with (
            patch.object(server, "WEBHOOK_DELAY_SECONDS", 0),
            patch.object(server, "WEBHOOK_RETRY_INTERVAL_SECONDS", 0),
            patch.object(server, "_sync_one_recorded", boom),
        ):
            asyncio.run(server._webhook_sync())
        assert calls == [True], "auto-sync is the safety net; don't hammer a broken backend"


class TestServerlessDeployment:
    """A serverless function is frozen at the response, so the staged retry
    cannot run there. The webhook must then do something safe rather than
    scheduling work that silently never happens."""

    @pytest.fixture
    def vercel_client(self):
        with patch.dict(os.environ, {"CRON_SECRET": "cron-123", "VERCEL": "1"}):
            from hevy2garmin.server import app
            yield TestClient(app)

    def test_background_work_is_not_scheduled_on_vercel(self, vercel_client) -> None:
        from hevy2garmin import server

        with (
            patch.object(server, "load_config", lambda: {"merge_mode": True}),
            patch("hevy2garmin.server._webhook_sync", new_callable=AsyncMock) as worker,
        ):
            resp = vercel_client.post(
                "/api/cron/webhook", headers={"Authorization": "Bearer cron-123"}
            )
        assert resp.status_code == 200
        worker.assert_not_called()

    def test_with_the_watch_merge_on_it_defers_to_cron(self, vercel_client) -> None:
        """Uploading now would create the duplicate the merge exists to avoid."""
        from hevy2garmin import server

        called = []

        async def fake_sync(**kw):
            called.append(kw)
            return JSONResponse({"synced": 1})

        with (
            patch.object(server, "load_config", lambda: {"merge_mode": True}),
            patch.object(server, "_sync_one_recorded", fake_sync),
        ):
            resp = vercel_client.post(
                "/api/cron/webhook", headers={"Authorization": "Bearer cron-123"}
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deferred"
        assert called == []

    def test_with_the_watch_merge_off_it_still_defers(self, vercel_client) -> None:
        """A saved setting cannot make the webhook upload a standalone FIT."""
        from hevy2garmin import server

        called = []

        async def fake_sync(**kw):
            called.append(kw)
            return JSONResponse({"synced": 1, "title": "Push"})

        with (
            patch.object(server, "load_config", lambda: {"merge_mode": False}),
            patch.object(server, "_sync_one_recorded", fake_sync),
        ):
            resp = vercel_client.post(
                "/api/cron/webhook", headers={"Authorization": "Bearer cron-123"}
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deferred"
        assert called == []

    def test_auth_is_still_enforced_on_serverless(self, vercel_client) -> None:
        assert vercel_client.post("/api/cron/webhook").status_code == 401

    def test_background_capability_is_derived_from_the_platform(self) -> None:
        from hevy2garmin.server import _can_run_background_work

        with patch.dict(os.environ, {"VERCEL": "1"}):
            assert _can_run_background_work() is False
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VERCEL", None)
            assert _can_run_background_work() is True
