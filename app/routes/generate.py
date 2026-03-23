import json
import uuid
from datetime import datetime
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from ..auth import get_spotify_client
from ..models import get_conn
from ..discover import run_discovery
from ..playlist import (get_liked_artists_and_tracks, get_playlist_artists_and_tracks,
                         search_track, push_to_spotify)
from sqlalchemy import text

generate_bp = Blueprint("generate", __name__)

@generate_bp.route("/start", methods=["POST"])
def start():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))

    seed_type = request.form.get("seed_type", "liked")
    seed_id = request.form.get("seed_id", "")
    seed_name = request.form.get("seed_name", "Liked Songs")
    playlist_id = str(uuid.uuid4())

    with get_conn() as conn:
        conn.execute(text("""
            INSERT INTO playlists (id, owner_id, title, seed_type, seed_name, track_data, status)
            VALUES (:id, :owner, :title, :stype, :sname, :tracks, 'pending')
        """), {
            "id": playlist_id,
            "owner": session["user_id"],
            "title": f"Rabbithole - {seed_name}",
            "stype": seed_type,
            "sname": seed_name,
            "tracks": "[]",
        })
        conn.commit()

    return redirect(url_for("generate.status", job_id=playlist_id))

@generate_bp.route("/status/<job_id>")
def status(job_id):
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    return render_template("generating.html", job_id=job_id)

@generate_bp.route("/poll/<job_id>")
def poll(job_id):
    if "user_id" not in session:
        return jsonify({"status": "error", "message": "Not logged in"})

    with get_conn() as conn:
        row = conn.execute(
            text("SELECT status, track_data, seed_type, seed_name FROM playlists WHERE id=:id AND owner_id=:uid"),
            {"id": job_id, "uid": session["user_id"]}
        ).fetchone()

    if not row:
        return jsonify({"status": "error", "message": "Playlist not found"})

    db_status, track_data, seed_type, seed_name = row

    if db_status == "done":
        return jsonify({"status": "done", "redirect": url_for("generate.result", playlist_id=job_id)})

    if db_status == "error":
        return jsonify({"status": "error", "message": "Discovery failed, please try again"})

    if db_status == "running":
        return jsonify({"status": "running", "message": "Finding your music..."})

    # Status is 'pending' - start the work now
    with get_conn() as conn:
        conn.execute(text("UPDATE playlists SET status='running' WHERE id=:id"), {"id": job_id})
        conn.commit()

    try:
        sp = get_spotify_client()
        if seed_type == "liked":
            liked_artists, liked_tracks = get_liked_artists_and_tracks(sp)
        else:
            liked_artists, liked_tracks = get_playlist_artists_and_tracks(sp, seed_type)

        discoveries = run_discovery(liked_artists, liked_tracks, target_artists=40, sample_size=15)

        # Search Spotify for each track
        found = []
        for d in discoveries[:100]:
            result = search_track(sp, d["artist"], d["track"])
            if result:
                found.append(result)

        with get_conn() as conn:
            conn.execute(text("""
                UPDATE playlists SET status='done', track_data=:tracks WHERE id=:id
            """), {"tracks": json.dumps(found), "id": job_id})
            conn.commit()

        return jsonify({"status": "done", "redirect": url_for("generate.result", playlist_id=job_id)})

    except Exception as e:
        with get_conn() as conn:
            conn.execute(text("UPDATE playlists SET status='error' WHERE id=:id"), {"id": job_id})
            conn.commit()
        return jsonify({"status": "error", "message": str(e)})

@generate_bp.route("/result/<playlist_id>")
def result(playlist_id):
    if "user_id" not in session:
        return redirect(url_for("auth.login"))

    with get_conn() as conn:
        row = conn.execute(
            text("SELECT title, track_data, is_published, spotify_playlist_id FROM playlists WHERE id=:id AND owner_id=:uid"),
            {"id": playlist_id, "uid": session["user_id"]}
        ).fetchone()

    if not row:
        return redirect(url_for("main.dashboard"))

    title, track_data, is_published, spotify_id = row
    tracks = json.loads(track_data)
    return render_template("result.html", playlist_id=playlist_id, title=title,
                           tracks=tracks, is_published=is_published, spotify_id=spotify_id)

@generate_bp.route("/push/<playlist_id>", methods=["POST"])
def push(playlist_id):
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"})

    with get_conn() as conn:
        row = conn.execute(
            text("SELECT title, track_data FROM playlists WHERE id=:id AND owner_id=:uid"),
            {"id": playlist_id, "uid": session["user_id"]}
        ).fetchone()

    if not row:
        return jsonify({"error": "Not found"})

    title, track_data = row
    tracks = json.loads(track_data)
    sp = get_spotify_client()

    url, spotify_id = push_to_spotify(sp, session["user_id"], tracks, title)

    with get_conn() as conn:
        conn.execute(text("UPDATE playlists SET spotify_playlist_id=:sid WHERE id=:id"),
                     {"sid": spotify_id, "id": playlist_id})
        conn.commit()

    return jsonify({"ok": True, "url": url})

@generate_bp.route("/publish/<playlist_id>", methods=["POST"])
def publish(playlist_id):
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"})

    with get_conn() as conn:
        conn.execute(text("""
            UPDATE playlists SET is_published=1, published_at=CURRENT_TIMESTAMP WHERE id=:id AND owner_id=:uid
        """), {"id": playlist_id, "uid": session["user_id"]})
        conn.commit()

    return jsonify({"ok": True})
