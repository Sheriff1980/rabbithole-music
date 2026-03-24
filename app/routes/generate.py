import json
import uuid
import logging
import threading
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from ..auth import get_spotify_client
from ..models import get_conn, engine
from ..discover import run_discovery, get_genre_seed_artists
from ..playlist import (get_liked_artists_and_tracks, get_playlist_artists_and_tracks,
                         search_artist_tracks, search_track, push_to_spotify)
from ..models import engine as db_engine
from ..utils import csrf_required, decrypt_token
from ..limiter import limiter
from sqlalchemy import text

logger = logging.getLogger(__name__)
generate_bp = Blueprint("generate", __name__)


def set_progress(job_id, pct, msg):
    with engine.connect() as conn:
        conn.execute(text("UPDATE playlists SET progress=:p, progress_msg=:m WHERE id=:id"),
                     {"p": pct, "m": msg, "id": job_id})
        conn.commit()


def _get_user_feedback(user_id):
    """Return (liked_artists set, disliked_artists set) from track_feedback table."""
    liked = set()
    disliked = set()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT artist, feedback FROM track_feedback WHERE user_id=:uid"),
            {"uid": user_id}
        ).fetchall()
    for artist, feedback in rows:
        if feedback == 1:
            liked.add(artist.lower())
        elif feedback == -1:
            disliked.add(artist.lower())
    return liked, disliked


def run_discovery_background(job_id, user_id, seed_type, access_token,
                              excluded_genres=None, playlist_size=60,
                              adventurousness=3, genre_spread=3,
                              deep_cuts=0, surprise_me=0):
    """Runs discovery in a background thread, updates DB when done."""
    import spotipy
    sp = spotipy.Spotify(auth=access_token)
    try:
        set_progress(job_id, 10, "Reading your library...")
        if seed_type == "liked":
            liked_artists, liked_tracks = get_liked_artists_and_tracks(sp, user_id=user_id, engine=db_engine)
        elif seed_type.startswith("artist:"):
            # "More like this" or artist search seed — single artist name
            artist_name = seed_type[7:]
            liked_artists = [artist_name]
            liked_tracks = set()
        elif seed_type.startswith("genre:"):
            # Genre seed — fetch top artists for this genre from Last.fm
            genre_name = seed_type[6:]
            set_progress(job_id, 15, f"Finding artists in {genre_name}...")
            liked_artists = get_genre_seed_artists(genre_name, limit=25)
            liked_tracks = set()
        else:
            liked_artists, liked_tracks = get_playlist_artists_and_tracks(sp, seed_type)

        # Load vote feedback to teach the algorithm
        voted_liked, voted_disliked = _get_user_feedback(user_id)

        # Map adventurousness (1-5) to discovery params
        if surprise_me:
            # Surprise Me: use aggressive discovery params
            sample_size, similar_limit = 60, 25
        else:
            adv_map = {1: (10, 5), 2: (15, 3), 3: (25, 10), 4: (35, 15), 5: (50, 20)}
            sample_size, similar_limit = adv_map.get(adventurousness, (25, 10))

        # Map genre_spread (1-5) to max_per_genre
        if surprise_me:
            max_per_genre = 2  # Maximum genre diversity
        else:
            spread_map = {1: 8, 2: 6, 3: 4, 4: 3, 5: 2}
            max_per_genre = spread_map.get(genre_spread, 4)

        target_artists = max(15, playlist_size // 2)

        progress_msg = "Surprise discovery in progress..." if surprise_me else "Finding similar artists via Last.fm..."
        if deep_cuts:
            progress_msg = "Digging for deep cuts..." if not surprise_me else "Surprise deep cut hunt..."
        set_progress(job_id, 30, progress_msg)

        def progress_fn(pct, msg):
            set_progress(job_id, pct, msg)

        discoveries = run_discovery(liked_artists, liked_tracks,
                                    target_artists=target_artists,
                                    sample_size=sample_size,
                                    similar_limit=similar_limit,
                                    excluded_genres=excluded_genres,
                                    max_per_genre=max_per_genre,
                                    voted_liked=voted_liked,
                                    voted_disliked=voted_disliked,
                                    surprise_me=bool(surprise_me),
                                    deep_cuts=bool(deep_cuts),
                                    progress_fn=progress_fn)

        set_progress(job_id, 70, "Matching tracks on Spotify...")
        seen_artists = {}
        for d in discoveries:
            seen_artists.setdefault(d["artist"], []).append(d["track"])

        found = []
        for artist_name in list(seen_artists.keys())[:target_artists]:
            tracks = search_artist_tracks(sp, artist_name, count=3, deep_cuts=bool(deep_cuts))
            found.extend(tracks)
            if len(found) >= playlist_size:
                break

        # Deep cuts + artist seed: also grab the seed artist's own deep cuts
        if deep_cuts and seed_type.startswith("artist:"):
            seed_artist = seed_type[7:]
            own_deep = search_artist_tracks(sp, seed_artist, count=5, deep_cuts=True)
            # Prepend seed artist's own deep cuts
            existing_uris = {t.get("uri") for t in found}
            for t in own_deep:
                if t.get("uri") not in existing_uris:
                    found.insert(0, t)

        set_progress(job_id, 95, "Almost done...")
        with engine.connect() as conn:
            conn.execute(text("UPDATE playlists SET status='done', track_data=:tracks, progress=100 WHERE id=:id"),
                         {"tracks": json.dumps(found), "id": job_id})
            conn.commit()

    except Exception:
        logger.exception("Discovery failed for job %s", job_id)
        with engine.connect() as conn:
            conn.execute(text("UPDATE playlists SET status='error' WHERE id=:id"), {"id": job_id})
            conn.commit()


@generate_bp.route("/start", methods=["POST"])
@csrf_required
@limiter.limit("10 per hour")
def start():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))

    seed_type   = request.form.get("seed_type", "liked")
    seed_name   = request.form.get("seed_name", "Liked Songs")
    custom_name = request.form.get("playlist_name", "").strip()
    excluded_raw = request.form.get("excluded_genres", "")
    excluded_genres = [g.strip().lower() for g in excluded_raw.split(",") if g.strip()]

    playlist_size   = min(100, max(20, int(request.form.get("playlist_size",   60))))
    adventurousness = min(5,   max(1,  int(request.form.get("adventurousness",  3))))
    genre_spread    = min(5,   max(1,  int(request.form.get("genre_spread",      3))))
    deep_cuts       = min(1,   max(0,  int(request.form.get("deep_cuts",         0))))
    surprise_me     = min(1,   max(0,  int(request.form.get("surprise_me",       0))))

    # Build title with feature tags
    base_name = seed_name
    if surprise_me and not seed_type.startswith("artist:") and not seed_type.startswith("genre:"):
        base_name = "Surprise Mix"
    title = custom_name if custom_name else f"Rabbithole - {base_name}"
    if deep_cuts and not custom_name:
        title += " (Deep Cuts)"
    if surprise_me and not custom_name:
        title += " (Surprise)"

    playlist_id = str(uuid.uuid4())

    with get_conn() as conn:
        conn.execute(text("""
            INSERT INTO playlists
              (id, owner_id, title, seed_type, seed_name, track_data, status,
               excluded_genres, playlist_size, adventurousness, genre_spread,
               deep_cuts, surprise_me)
            VALUES (:id, :owner, :title, :stype, :sname, :tracks, 'pending',
                    :excl, :psize, :adv, :spread, :dc, :sm)
        """), {
            "id": playlist_id, "owner": session["user_id"], "title": title,
            "stype": seed_type, "sname": seed_name, "tracks": "[]",
            "excl": ",".join(excluded_genres),
            "psize": playlist_size, "adv": adventurousness, "spread": genre_spread,
            "dc": deep_cuts, "sm": surprise_me,
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
            text("SELECT status, track_data, seed_type, seed_name, progress, progress_msg FROM playlists WHERE id=:id AND owner_id=:uid"),
            {"id": job_id, "uid": session["user_id"]}
        ).fetchone()

    if not row:
        return jsonify({"status": "error", "message": "Playlist not found"})

    db_status, track_data, seed_type, seed_name, progress, progress_msg = row

    if db_status == "done":
        return jsonify({"status": "done", "redirect": url_for("generate.result", playlist_id=job_id)})

    if db_status == "error":
        return jsonify({"status": "error", "message": "Discovery failed, please try again"})

    if db_status == "running":
        return jsonify({"status": "running", "message": progress_msg or "Finding your music...", "progress": progress or 5})

    # Status is 'pending' — kick off background thread
    with get_conn() as conn:
        conn.execute(text("UPDATE playlists SET status='running' WHERE id=:id"), {"id": job_id})
        row2 = conn.execute(
            text("""SELECT u.access_token, p.excluded_genres, p.playlist_size,
                           p.adventurousness, p.genre_spread, p.deep_cuts, p.surprise_me
                    FROM users u JOIN playlists p ON p.owner_id=u.id WHERE p.id=:jid"""),
            {"jid": job_id}
        ).fetchone()
        conn.commit()

    access_token  = decrypt_token(row2[0]) if row2 else None
    excl          = [g for g in (row2[1] or "").split(",") if g] if row2 else []
    psize         = row2[2] if row2 else 60
    adv           = row2[3] if row2 else 3
    spread        = row2[4] if row2 else 3
    dc            = row2[5] if row2 else 0
    sm            = row2[6] if row2 else 0

    t = threading.Thread(target=run_discovery_background,
                         args=(job_id, session["user_id"], seed_type, access_token,
                               excl, psize, adv, spread, dc, sm),
                         daemon=True)
    t.start()
    return jsonify({"status": "running", "message": "Discovery started, searching for music..."})


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
@csrf_required
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
    all_tracks = {t["uri"]: t for t in json.loads(track_data) if t.get("uri")}
    sp = get_spotify_client()

    body = request.get_json(silent=True) or {}
    ordered_uris = body.get("uris")
    if ordered_uris:
        tracks = [all_tracks[u] for u in ordered_uris if u in all_tracks]
    else:
        tracks = list(all_tracks.values())

    url, spotify_id = push_to_spotify(sp, session["user_id"], tracks, title)

    with get_conn() as conn:
        conn.execute(text("UPDATE playlists SET spotify_playlist_id=:sid WHERE id=:id"),
                     {"sid": spotify_id, "id": playlist_id})
        conn.commit()

    return jsonify({"ok": True, "url": url})


@generate_bp.route("/reroll/<playlist_id>", methods=["POST"])
@csrf_required
@limiter.limit("10 per hour")
def reroll(playlist_id):
    """Clone settings from an existing playlist and kick off a new generation."""
    if "user_id" not in session:
        return jsonify({"ok": False})

    with get_conn() as conn:
        row = conn.execute(
            text("""SELECT seed_type, seed_name, excluded_genres,
                           playlist_size, adventurousness, genre_spread,
                           deep_cuts, surprise_me
                    FROM playlists WHERE id=:id AND owner_id=:uid"""),
            {"id": playlist_id, "uid": session["user_id"]}
        ).fetchone()

    if not row:
        return jsonify({"ok": False})

    seed_type, seed_name, excl, psize, adv, spread, dc, sm = row
    new_id = str(uuid.uuid4())

    with get_conn() as conn:
        conn.execute(text("""
            INSERT INTO playlists
              (id, owner_id, title, seed_type, seed_name, track_data, status,
               excluded_genres, playlist_size, adventurousness, genre_spread,
               deep_cuts, surprise_me)
            VALUES (:id, :owner, :title, :stype, :sname, '[]', 'pending',
                    :excl, :psize, :adv, :spread, :dc, :sm)
        """), {
            "id": new_id, "owner": session["user_id"],
            "title": f"Rabbithole - {seed_name} (Re-roll)",
            "stype": seed_type, "sname": seed_name,
            "excl": excl, "psize": psize, "adv": adv, "spread": spread,
            "dc": dc or 0, "sm": sm or 0,
        })
        conn.commit()

    with get_conn() as conn:
        token_row = conn.execute(
            text("SELECT access_token FROM users WHERE id=:uid"),
            {"uid": session["user_id"]}
        ).fetchone()

    excl_list = [g for g in (excl or "").split(",") if g]
    t = threading.Thread(
        target=run_discovery_background,
        args=(new_id, session["user_id"], seed_type, decrypt_token(token_row[0]),
              excl_list, psize, adv, spread, dc or 0, sm or 0),
        daemon=True
    )
    t.start()
    return jsonify({"ok": True, "new_id": new_id})


@generate_bp.route("/from-track", methods=["POST"])
@csrf_required
@limiter.limit("10 per hour")
def from_track():
    """Start a new generation seeded from a single artist ('More like this')."""
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"})

    body = request.get_json(silent=True) or {}
    artist    = body.get("artist", "").strip()
    source_id = body.get("source_playlist_id", "")

    if not artist:
        return jsonify({"error": "No artist provided"})

    playlist_size = 60
    adventurousness = 3
    genre_spread = 3
    excluded_genres = []

    if source_id:
        with get_conn() as conn:
            row = conn.execute(text("""
                SELECT playlist_size, adventurousness, genre_spread, excluded_genres
                FROM playlists WHERE id=:id AND owner_id=:uid
            """), {"id": source_id, "uid": session["user_id"]}).fetchone()
            if row:
                playlist_size   = row[0]
                adventurousness = row[1]
                genre_spread    = row[2]
                excluded_genres = [g for g in (row[3] or "").split(",") if g]

    new_id = str(uuid.uuid4())
    title  = f"Rabbithole - More like {artist}"

    with get_conn() as conn:
        conn.execute(text("""
            INSERT INTO playlists
              (id, owner_id, title, seed_type, seed_name, track_data, status,
               excluded_genres, playlist_size, adventurousness, genre_spread)
            VALUES (:id, :owner, :title, :stype, :sname, '[]', 'pending',
                    :excl, :psize, :adv, :spread)
        """), {
            "id": new_id, "owner": session["user_id"], "title": title,
            "stype": f"artist:{artist}", "sname": artist,
            "excl": ",".join(excluded_genres),
            "psize": playlist_size, "adv": adventurousness, "spread": genre_spread,
        })
        conn.commit()

    return jsonify({"ok": True, "job_id": new_id})


@generate_bp.route("/toggle-refresh/<playlist_id>", methods=["POST"])
@csrf_required
def toggle_refresh(playlist_id):
    """Toggle weekly auto-refresh on/off for a playlist."""
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"})

    with get_conn() as conn:
        row = conn.execute(text(
            "SELECT auto_refresh FROM playlists WHERE id=:id AND owner_id=:uid"
        ), {"id": playlist_id, "uid": session["user_id"]}).fetchone()

        if not row:
            return jsonify({"error": "Not found"}), 404

        new_val = 0 if row[0] else 1
        conn.execute(text(
            "UPDATE playlists SET auto_refresh=:v WHERE id=:id AND owner_id=:uid"
        ), {"v": new_val, "id": playlist_id, "uid": session["user_id"]})
        conn.commit()

    return jsonify({"ok": True, "auto_refresh": new_val})


@generate_bp.route("/publish/<playlist_id>", methods=["POST"])
@csrf_required
def publish(playlist_id):
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"})

    with get_conn() as conn:
        conn.execute(text("""
            UPDATE playlists SET is_published=1, published_at=CURRENT_TIMESTAMP WHERE id=:id AND owner_id=:uid
        """), {"id": playlist_id, "uid": session["user_id"]})
        conn.commit()

    return jsonify({"ok": True})
