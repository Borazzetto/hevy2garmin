"""Enrich existing Garmin-pushed Strava activities; never upload or delete."""
from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import timedelta

import requests

from hevy2garmin._isotime import parse_iso
from hevy2garmin.garmin import generate_description

logger = logging.getLogger("hevy2garmin")
API = "https://www.strava.com/api/v3"
BEGIN = "[Hevy workout]"
END = "[/Hevy workout]"
TOKEN_KEY = "strava_oauth_tokens"


def oauth_settings():
    return {key: os.environ.get(env, "").strip() for key, env in (
        ("client_id", "STRAVA_CLIENT_ID"),
        ("client_secret", "STRAVA_CLIENT_SECRET"),
        ("redirect_uri", "STRAVA_REDIRECT_URI"),
    )}


def configured():
    return all(oauth_settings().values())


def exchange_token(store, **grant):
    settings = oauth_settings()
    response = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": settings["client_id"],
        "client_secret": settings["client_secret"], **grant,
    }, timeout=15)
    if response.status_code != 200:
        raise RuntimeError("Strava authorization failed; reconnect the account")
    tokens = response.json()
    if not all(tokens.get(k) for k in ("access_token", "refresh_token", "expires_at")):
        raise RuntimeError("Strava returned incomplete credentials")
    previous = store.get_app_config(TOKEN_KEY) or {}
    tokens["athlete"] = tokens.get("athlete", previous.get("athlete", {}))
    store.set_app_config(TOKEN_KEY, tokens)
    return tokens


class StravaClient:
    def __init__(self, store):
        self.store = store

    def request(self, method, path, **kwargs):
        tokens = self.store.get_app_config(TOKEN_KEY) or {}
        if not tokens.get("access_token"):
            raise RuntimeError("Strava account is not connected")
        if tokens.get("expires_at", 0) <= time.time() + 60:
            tokens = exchange_token(self.store, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        response = requests.request(method, API + path, headers={
            "Authorization": "Bearer " + tokens["access_token"],
        }, timeout=15, **kwargs)
        if not response.ok:
            # Do not include response bodies, tokens or request URLs in logs.
            raise RuntimeError(f"Strava request failed ({response.status_code})")
        return response.json()

    def find(self, workout, garmin_id):
        start = parse_iso(workout["start_time"])
        params = {"after": int((start - timedelta(days=1)).timestamp()),
                  "before": int((start + timedelta(days=1)).timestamp()), "per_page": 100}
        candidates = []
        for page in range(1, 4):
            rows = self.request("GET", "/athlete/activities", params={**params, "page": page})
            candidates.extend(a for a in rows if a.get("external_id") == f"garmin_push_{garmin_id}")
            if len(rows) < 100:
                break
        # Ambiguous or absent IDs must never fall back to a time-only match.
        if len(candidates) != 1:
            return None
        activity = self.request("GET", f"/activities/{candidates[0]['id']}")
        if activity.get("external_id") != f"garmin_push_{garmin_id}":
            return None
        return activity


def enrich(store, workout, garmin_id, client=None):
    """Update a verified existing activity, preserving user notes and metrics."""
    if not garmin_id:
        return "pending"
    client = client or StravaClient(store)
    key = "strava_workout_" + str(workout["id"])
    previous = store.get_app_config(key) or {}
    summary = generate_description(workout)
    digest = hashlib.sha256((str(garmin_id) + summary).encode()).hexdigest()
    if previous.get("digest") == digest:
        return "unchanged"
    activity = client.find(workout, garmin_id)
    if not activity:
        return "pending"
    notes = activity.get("description") or ""
    if BEGIN in notes or END in notes:
        if notes.count(BEGIN) != 1 or notes.count(END) != 1 or notes.index(END) < notes.index(BEGIN):
            return "needs_review"
        left, rest = notes.split(BEGIN, 1)
        _, right = rest.split(END, 1)
        notes = (left.rstrip() + "\n\n" + right.lstrip()).strip()
    block = f"{BEGIN}\n{summary}\n{END}"
    description = f"{notes}\n\n{block}".strip()
    if len(description) > 10000:
        return "needs_review"
    body = {"description": description}
    # Rename on the first enhancement; preserve later user title edits.
    if not previous or activity.get("name") == previous.get("managed_name"):
        body["name"] = workout.get("title") or "Workout"
    if not previous:
        store.set_app_config(key + "_backup", {
            "activity_id": activity["id"], "name": activity.get("name"),
            "description": activity.get("description"),
        })
    updated = client.request("PUT", f"/activities/{activity['id']}", json=body)
    if updated.get("id") != activity["id"] or updated.get("description") != description:
        raise RuntimeError("Strava did not confirm the description update")
    store.set_app_config(key, {"digest": digest, "activity_id": activity["id"],
                              "garmin_id": str(garmin_id), "hevy_updated_at": workout.get("updated_at"),
                              "managed_name": body.get("name", previous.get("managed_name"))})
    return "updated"


def sync_recent(store, hevy, limit=5):
    """Retry/backfill recent merges even when Garmin marks them already synced."""
    if not configured() or not (store.get_app_config(TOKEN_KEY) or {}).get("access_token"):
        return {"connected": False, "updated": 0}
    result = {"connected": True, "updated": 0, "pending": 0, "failed": 0}
    attempts = 0
    for row in store.get_recent_synced(limit=50):
        if row.get("sync_method") != "merge" or not row.get("garmin_activity_id"):
            continue
        previous = store.get_app_config("strava_workout_" + str(row["hevy_id"])) or {}
        if (previous.get("digest") and previous.get("garmin_id") == str(row["garmin_activity_id"])
                and previous.get("hevy_updated_at") == row.get("hevy_updated_at")):
            continue
        try:
            workout = hevy.get_workout(row["hevy_id"])
            if not workout:
                continue
            status = enrich(store, workout, row["garmin_activity_id"])
            if status == "unchanged":
                continue
            attempts += 1
            if status == "updated":
                result["updated"] += 1
            else:
                result["pending"] += 1
        except Exception:
            result["failed"] += 1
            logger.warning("Strava enrichment failed; will retry on the next sync")
            break  # Avoid repeatedly hitting an expired permission or rate limit.
        if attempts >= limit:
            break
    return result


def sync_recent_safely(store, hevy, limit=5):
    try:
        return sync_recent(store, hevy, limit=limit)
    except Exception:
        logger.warning("Strava enrichment unavailable; Garmin sync remains saved")
        return {"failed": 1}
