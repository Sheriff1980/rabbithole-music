import json
import uuid
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from ..auth import get_spotify_client
from ..models import get_conn
from ..playlist import push_to_spotify
from ..utils import csrf_required
from ..limiter import limiter
from sqlalchemy import text

community_bp = Blueprint("community", __name__)

@community_bp.route("/")
def index():
    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT p.id, p.title, p.seed_name, p.upvotes, p.downvotes,
                   p.created_at, u.display_name, u.avatar_url,
                   p.track_data
            FROM playlists p JOIN users u ON p.owner_id = u.id
            WHERE p.is_published = 1
            ORDER BY (p.upvotes - p.downvotes) DESC, p.published_at DESC
            LIMIT 50
        """)).fetchall()

    playlists = []
    for r in rows:
        tracks = json.loads(r[8])
        playlists.append({
            "id": r[0], "title": r[1], "seed_name": r[2],
            "upvotes": r[3], "downvotes": r[4], "created_at": r[5],
            "creator": r[6], "avatar": r[7],
            "track_count": len(tracks),
            "preview_tracks": tracks[:3],
        })

    user_votes = {}
    if "user_id" in session:
        with get_conn() as conn:
            vote_rows = conn.execute(text("""
                SELECT playlist_id, vote FROM playlist_votes WHERE user_id=:uid
            """), {"uid": session["user_id"]}).fetchall()
            user_votes = {r[0]: r[1] for r in vote_rows}

    return render_template("community.html", playlists=playlists, user_votes=user_votes)

@community_bp.route("/playlist/<playlist_id>")
def detail(playlist_id):
    with get_conn() as conn:
        row = conn.execute(text("""
            SELECT p.id, p.title, p.seed_name, p.upvotes, p.downvotes,
                   p.track_data, u.display_name, u.avatar_url
            FROM playlists p JOIN users u ON p.owner_id = u.id
            WHERE p.id=:id AND p.is_published=1
        """), {"id": playlist_id}).fetchone()

    if not row:
        return redirect(url_for("community.index"))

    tracks = json.loads(row[5])

    # Get track vote totals
    with get_conn() as conn:
        tvotes = conn.execute(text("""
            SELECT track_uri, SUM(vote) as score FROM track_votes
            WHERE playlist_id=:pid GROUP BY track_uri
        """), {"pid": playlist_id}).fetchall()
    track_scores = {r[0]: r[1] for r in tvotes}

    # User's own votes
    user_playlist_vote = None
    user_track_votes = {}
    if "user_id" in session:
        with get_conn() as conn:
            pv = conn.execute(text(
                "SELECT vote FROM playlist_votes WHERE user_id=:uid AND playlist_id=:pid"
            ), {"uid": session["user_id"], "pid": playlist_id}).fetchone()
            user_playlist_vote = pv[0] if pv else None

            tv_rows = conn.execute(text(
                "SELECT track_uri, vote FROM track_votes WHERE user_id=:uid AND playlist_id=:pid"
            ), {"uid": session["user_id"], "pid": playlist_id}).fetchall()
            user_track_votes = {r[0]: r[1] for r in tv_rows}

    playlist = {
        "id": row[0], "title": row[1], "seed_name": row[2],
        "upvotes": row[3], "downvotes": row[4],
        "creator": row[6], "avatar": row[7],
    }

    return render_template("playlist_detail.html", playlist=playlist, tracks=tracks,
                           track_scores=track_scores, user_playlist_vote=user_playlist_vote,
                           user_track_votes=user_track_votes)

@community_bp.route("/vote/<playlist_id>", methods=["POST"])
@csrf_required
@limiter.limit("60 per minute")
def vote(playlist_id):
    if "user_id" not in session:
        return jsonify({"error": "Login required"})

    raw = request.json.get("vote", 1) if request.json else 1
    vote_val = int(raw)
    if vote_val not in (1, -1):
        return jsonify({"error": "Invalid vote value"}), 400
    uid = session["user_id"]

    with get_conn() as conn:
        existing = conn.execute(text(
            "SELECT vote FROM playlist_votes WHERE user_id=:uid AND playlist_id=:pid"
        ), {"uid": uid, "pid": playlist_id}).fetchone()

        if existing:
            old_vote = existing[0]
            if old_vote == vote_val:
                # Remove vote (toggle off)
                conn.execute(text("DELETE FROM playlist_votes WHERE user_id=:uid AND playlist_id=:pid"),
                             {"uid": uid, "pid": playlist_id})
                delta_up = -1 if vote_val == 1 else 0
                delta_down = -1 if vote_val == -1 else 0
            else:
                # Change vote
                conn.execute(text("UPDATE playlist_votes SET vote=:v WHERE user_id=:uid AND playlist_id=:pid"),
                             {"v": vote_val, "uid": uid, "pid": playlist_id})
                delta_up = 1 if vote_val == 1 else -1
                delta_down = 1 if vote_val == -1 else -1
        else:
            conn.execute(text("INSERT INTO playlist_votes (user_id, playlist_id, vote) VALUES (:uid,:pid,:v)"),
                         {"uid": uid, "pid": playlist_id, "v": vote_val})
            delta_up = 1 if vote_val == 1 else 0
            delta_down = 1 if vote_val == -1 else 0

        conn.execute(text("""
            UPDATE playlists SET upvotes=upvotes+:du, downvotes=downvotes+:dd WHERE id=:pid
        """), {"du": delta_up, "dd": delta_down, "pid": playlist_id})
        conn.commit()

        updated = conn.execute(text(
            "SELECT upvotes, downvotes FROM playlists WHERE id=:pid"
        ), {"pid": playlist_id}).fetchone()

    return jsonify({"ok": True, "upvotes": updated[0], "downvotes": updated[1]})

@community_bp.route("/track-vote/<playlist_id>/<path:track_uri>", methods=["POST"])
@csrf_required
@limiter.limit("60 per minute")
def track_vote(playlist_id, track_uri):
    if "user_id" not in session:
        return jsonify({"error": "Login required"})

    raw = request.json.get("vote", 1) if request.json else 1
    vote_val = int(raw)
    if vote_val not in (1, -1):
        return jsonify({"error": "Invalid vote value"}), 400
    uid = session["user_id"]

    with get_conn() as conn:
        conn.execute(text("""
            INSERT INTO track_votes (id, user_id, playlist_id, track_uri, vote)
            VALUES (:id, :uid, :pid, :uri, :v)
            ON CONFLICT(user_id, playlist_id, track_uri) DO UPDATE SET vote=excluded.vote
        """), {"id": str(uuid.uuid4()), "uid": uid, "pid": playlist_id, "uri": track_uri, "v": vote_val})
        conn.commit()

        score = conn.execute(text(
            "SELECT SUM(vote) FROM track_votes WHERE playlist_id=:pid AND track_uri=:uri"
        ), {"pid": playlist_id, "uri": track_uri}).fetchone()

    return jsonify({"ok": True, "score": score[0] or 0})

@community_bp.route("/import/<playlist_id>", methods=["POST"])
@csrf_required
def import_playlist(playlist_id):
    if "user_id" not in session:
        return jsonify({"error": "Login required"})

    with get_conn() as conn:
        row = conn.execute(text(
            "SELECT title, track_data FROM playlists WHERE id=:id AND is_published=1"
        ), {"id": playlist_id}).fetchone()

    if not row:
        return jsonify({"error": "Not found"})

    title, track_data = row
    tracks = json.loads(track_data)
    sp = get_spotify_client()
    url, _ = push_to_spotify(sp, session["user_id"], tracks, f"{title} (imported)")
    return jsonify({"ok": True, "url": url})
