import requests as req
from flask import Blueprint, request, jsonify, session
from ..models import get_conn
from ..utils import csrf_required
from ..limiter import limiter
from sqlalchemy import text

feedback_bp = Blueprint("feedback", __name__)


@feedback_bp.route("/feedback/track", methods=["POST"])
@csrf_required
@limiter.limit("120 per minute")
def track_feedback():
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    data   = request.get_json() or {}
    artist = (data.get("artist") or "").strip()
    track  = (data.get("track")  or "").strip()
    value  = data.get("value", 0)

    try:
        value = int(value)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid value"}), 400

    if not artist or not track or value not in (1, -1):
        return jsonify({"ok": False}), 400

    with get_conn() as conn:
        conn.execute(text("""
            INSERT INTO track_feedback (user_id, artist, track_name, feedback)
            VALUES (:uid, :artist, :track, :val)
            ON CONFLICT(user_id, artist, track_name)
            DO UPDATE SET feedback=:val
        """), {"uid": session["user_id"], "artist": artist, "track": track, "val": value})
        conn.commit()

    return jsonify({"ok": True})


@feedback_bp.route("/preview")
@limiter.limit("60 per minute")
def preview():
    """Fetch a 30-sec preview URL from Deezer by artist + track name. Login required."""
    if "user_id" not in session:
        return jsonify({"url": None}), 401

    artist = request.args.get("artist", "")
    track  = request.args.get("track", "")
    if not artist or not track:
        return jsonify({"url": None})

    try:
        resp = req.get("https://api.deezer.com/search", params={
            "q": f'artist:"{artist}" track:"{track}"',
            "limit": 1,
        }, timeout=5)
        data  = resp.json()
        items = data.get("data", [])
        url   = items[0].get("preview") if items else None
        return jsonify({"url": url})
    except Exception:
        return jsonify({"url": None})
