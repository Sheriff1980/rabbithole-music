"""Admin routes for managing featured artists and submissions."""
import json
import uuid
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from ..auth import get_spotify_client
from ..models import get_conn
from ..utils import admin_required, csrf_required
from ..limiter import limiter
from sqlalchemy import text

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/featured")
@admin_required
def featured_list():
    with get_conn() as conn:
        artists = conn.execute(text(
            "SELECT id, artist_name, image_url, is_active, queue_position, activated_at, created_at "
            "FROM featured_artists ORDER BY queue_position ASC"
        )).fetchall()
        sub_count = conn.execute(text(
            "SELECT COUNT(*) FROM featured_submissions WHERE status='pending'"
        )).fetchone()[0]

    items = []
    for r in artists:
        items.append({
            "id": r[0], "name": r[1], "image": r[2],
            "is_active": r[3], "position": r[4],
            "activated_at": r[5], "created_at": r[6],
        })
    return render_template("admin/featured_list.html",
                           artists=items, pending_count=sub_count)


@admin_bp.route("/featured/search-spotify")
@admin_required
def search_spotify_artist():
    """AJAX endpoint: search Spotify for an artist and return top tracks."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"results": []})

    sp = get_spotify_client()
    if not sp:
        return jsonify({"error": "Not logged into Spotify"}), 401

    try:
        results = sp.search(q=q, type="artist", limit=5)
        artists = []
        for a in results["artists"]["items"]:
            artists.append({
                "name": a["name"],
                "id": a["id"],
                "uri": a["uri"],
                "image": a["images"][0]["url"] if a.get("images") else None,
                "followers": a["followers"]["total"],
            })
        return jsonify({"results": artists})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/featured/get-tracks/<artist_id>")
@admin_required
def get_artist_tracks(artist_id):
    """AJAX endpoint: get top 3 tracks for a Spotify artist."""
    sp = get_spotify_client()
    if not sp:
        return jsonify({"error": "Not logged into Spotify"}), 401

    try:
        top = sp.artist_top_tracks(artist_id, country="US")
        songs = []
        for t in top["tracks"][:3]:
            songs.append({
                "name": t["name"],
                "spotify_uri": t["uri"],
                "preview_url": t.get("preview_url"),
                "album_name": t["album"]["name"],
                "album_art": t["album"]["images"][1]["url"] if len(t["album"]["images"]) > 1 else None,
            })
        return jsonify({"songs": songs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/featured/add", methods=["GET", "POST"])
@admin_required
def featured_add():
    if request.method == "GET":
        return render_template("admin/featured_form.html", artist=None)

    # POST — save the featured artist
    artist_name = request.form.get("artist_name", "").strip()
    spotify_artist_id = request.form.get("spotify_artist_id", "")
    spotify_uri = request.form.get("spotify_uri", "")
    image_url = request.form.get("image_url", "")
    bio = request.form.get("bio", "").strip()
    songs_json = request.form.get("songs", "[]")

    if not artist_name:
        return redirect(url_for("admin.featured_add"))

    # Get next queue position
    with get_conn() as conn:
        max_pos = conn.execute(text(
            "SELECT COALESCE(MAX(queue_position), 0) FROM featured_artists"
        )).fetchone()[0]

        conn.execute(text("""
            INSERT INTO featured_artists
              (id, artist_name, spotify_artist_id, spotify_uri, image_url, bio,
               songs, queue_position, added_by)
            VALUES (:id, :name, :sid, :uri, :img, :bio, :songs, :pos, :admin)
        """), {
            "id": str(uuid.uuid4()),
            "name": artist_name,
            "sid": spotify_artist_id,
            "uri": spotify_uri,
            "img": image_url,
            "bio": bio,
            "songs": songs_json,
            "pos": max_pos + 1,
            "admin": session.get("user_id"),
        })
        conn.commit()

    return redirect(url_for("admin.featured_list"))


@admin_bp.route("/featured/activate/<artist_id>", methods=["POST"])
@admin_required
@csrf_required
def featured_activate(artist_id):
    with get_conn() as conn:
        # Deactivate all
        conn.execute(text("UPDATE featured_artists SET is_active=0"))
        # Activate this one
        conn.execute(text(
            "UPDATE featured_artists SET is_active=1, activated_at=CURRENT_TIMESTAMP WHERE id=:id"
        ), {"id": artist_id})
        conn.commit()
    return jsonify({"ok": True})


@admin_bp.route("/featured/delete/<artist_id>", methods=["POST"])
@admin_required
@csrf_required
def featured_delete(artist_id):
    with get_conn() as conn:
        conn.execute(text("DELETE FROM featured_artists WHERE id=:id"), {"id": artist_id})
        conn.commit()
    return jsonify({"ok": True})


# ── Submissions ──────────────────────────────────────────────────────────────

@admin_bp.route("/submissions")
@admin_required
def submissions_list():
    with get_conn() as conn:
        rows = conn.execute(text(
            "SELECT id, artist_name, spotify_url, contact_email, message, status, created_at "
            "FROM featured_submissions ORDER BY created_at DESC"
        )).fetchall()

    subs = []
    for r in rows:
        subs.append({
            "id": r[0], "name": r[1], "spotify_url": r[2],
            "email": r[3], "message": r[4], "status": r[5], "created_at": r[6],
        })
    return render_template("admin/submissions_list.html", submissions=subs)


@admin_bp.route("/submissions/approve/<sub_id>", methods=["POST"])
@admin_required
@csrf_required
def submission_approve(sub_id):
    with get_conn() as conn:
        conn.execute(text(
            "UPDATE featured_submissions SET status='approved', reviewed_by=:admin WHERE id=:id"
        ), {"id": sub_id, "admin": session.get("user_id")})
        conn.commit()
    return jsonify({"ok": True})


@admin_bp.route("/submissions/reject/<sub_id>", methods=["POST"])
@admin_required
@csrf_required
def submission_reject(sub_id):
    with get_conn() as conn:
        conn.execute(text(
            "UPDATE featured_submissions SET status='rejected', reviewed_by=:admin WHERE id=:id"
        ), {"id": sub_id, "admin": session.get("user_id")})
        conn.commit()
    return jsonify({"ok": True})
