import json
from flask import Blueprint, render_template, session, redirect, url_for, jsonify, request
from ..auth import get_spotify_client
from ..models import get_conn
from ..limiter import limiter
from sqlalchemy import text
import requests, os

main_bp = Blueprint("main", __name__)

# Tags that are metadata/social, not genres — filter these out
NON_GENRE_TAGS = {
    "seen live", "favorites", "favourite", "love", "loved", "awesome", "good",
    "great", "best", "beautiful", "amazing", "cool", "nice", "sexy", "sad",
    "happy", "chill", "mellow", "relax", "relaxing", "sleep", "workout",
    "party", "summer", "winter", "spring", "autumn", "road trip", "driving",
    "study", "focus", "morning", "night", "rainy day", "sunshine",
    "owned on vinyl", "under 2000 listeners", "all", "spotify", "youtube",
    "2000s", "2010s", "1990s", "1980s", "1970s", "1960s", "00s", "90s", "80s",
    "70s", "60s", "american", "british", "german", "french", "swedish",
    "canadian", "australian", "japanese", "korean", "female vocalists",
    "male vocalists", "vocals", "guitar", "piano", "instrumental",
    "singer-songwriter",
}

def fetch_lastfm_genres(limit=200):
    """Fetch top tags from Last.fm and filter to just genre-like ones."""
    try:
        resp = requests.get("http://ws.audioscrobbler.com/2.0/", params={
            "method": "chart.getTopTags",
            "api_key": os.getenv("LASTFM_API_KEY"),
            "format": "json",
            "limit": limit,
        }, timeout=5)
        tags = resp.json().get("tags", {}).get("tag", [])
        genres = []
        for t in tags:
            name = t["name"].lower().strip()
            if name not in NON_GENRE_TAGS and len(name) > 1 and not name.isdigit():
                genres.append(t["name"])
        return genres
    except Exception:
        return []

@main_bp.route("/api/search")
@limiter.limit("30 per minute")
def api_search():
    """Autocomplete search for artists and genres via Last.fm."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"artists": [], "genres": []})

    # Genre matches — filter from known genre list (fast, no API call)
    all_genres = fetch_lastfm_genres(limit=200)
    genre_matches = [g for g in all_genres if q.lower() in g.lower()][:5]

    # Artist search via Last.fm
    artist_matches = []
    try:
        resp = requests.get("http://ws.audioscrobbler.com/2.0/", params={
            "method": "artist.search", "artist": q,
            "api_key": os.getenv("LASTFM_API_KEY"), "format": "json", "limit": 6,
        }, timeout=5)
        results = resp.json().get("results", {}).get("artistmatches", {}).get("artist", [])
        for a in results:
            listeners = int(a.get("listeners", "0"))
            artist_matches.append({
                "name": a["name"],
                "listeners": f"{listeners:,}",
            })
    except Exception:
        pass

    return jsonify({"artists": artist_matches, "genres": genre_matches})


@main_bp.route("/health")
def health():
    """Lightweight keep-alive endpoint — no DB, no auth, instant 200."""
    return jsonify({"status": "ok"}), 200


@main_bp.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("main.dashboard"))
    return render_template("index.html")

@main_bp.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("auth.login"))

    # Get user's playlists
    playlists = []
    results = sp.current_user_playlists(limit=50)
    while results:
        for p in results["items"]:
            if p and p.get("name"):
                playlists.append({
                    "id": p["id"],
                    "name": p["name"],
                    "track_count": p["tracks"]["total"],
                    "image": p["images"][0]["url"] if p.get("images") else None,
                })
        results = sp.next(results) if results.get("next") else None

    # Fetch genres from Last.fm
    all_genres = fetch_lastfm_genres(limit=200)
    quick_genres = ["jazz", "classical", "metal", "hip-hop", "country",
                    "electronic", "r&b", "folk", "ambient", "reggae",
                    "bluegrass", "punk"]

    return render_template("dashboard.html",
                           playlists=playlists,
                           quick_genres=quick_genres,
                           all_genres=all_genres)


@main_bp.route("/history")
def history():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))

    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT id, title, seed_name, seed_type, status, track_data,
                   spotify_playlist_id, is_published, created_at,
                   playlist_size, adventurousness, genre_spread, auto_refresh
            FROM playlists
            WHERE owner_id=:uid AND status IN ('done', 'error')
            ORDER BY created_at DESC
        """), {"uid": session["user_id"]}).fetchall()

    playlists = []
    for r in rows:
        track_count = 0
        if r[5] and r[4] == "done":
            try:
                track_count = len(json.loads(r[5]))
            except Exception:
                pass
        playlists.append({
            "id":           r[0],
            "title":        r[1],
            "seed_name":    r[2],
            "seed_type":    r[3],
            "status":       r[4],
            "track_count":  track_count,
            "spotify_id":   r[6],
            "is_published": r[7],
            "created_at":   r[8],
            "size":         r[9],
            "adv":          r[10],
            "spread":       r[11],
            "auto_refresh": r[12],
        })

    return render_template("history.html", playlists=playlists)


@main_bp.route("/stats")
def stats():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))

    with get_conn() as conn:
        rows = conn.execute(text("""
            SELECT track_data, seed_name, created_at
            FROM playlists
            WHERE owner_id=:uid AND status='done'
            ORDER BY created_at DESC
        """), {"uid": session["user_id"]}).fetchall()

    # Aggregate stats
    total_playlists = len(rows)
    all_artists = set()
    all_uris = set()
    genre_counts = {}
    source_counts = {}
    track_type_counts = {}
    top_artists = {}  # artist -> count of appearances

    for track_data_raw, seed_name, created_at in rows:
        try:
            tracks = json.loads(track_data_raw)
        except Exception:
            continue
        for t in tracks:
            artist = t.get("artist", "").strip()
            if artist:
                all_artists.add(artist.lower())
                top_artists[artist] = top_artists.get(artist, 0) + 1
            if t.get("uri"):
                all_uris.add(t["uri"])
            prov = t.get("provenance", {})
            if prov.get("genre") and prov["genre"] != "other":
                g = prov["genre"]
                genre_counts[g] = genre_counts.get(g, 0) + 1
            if prov.get("source"):
                s = prov["source"]
                source_counts[s] = source_counts.get(s, 0) + 1
            if prov.get("track_type"):
                tt = prov["track_type"]
                track_type_counts[tt] = track_type_counts.get(tt, 0) + 1

    # Sort top artists by count
    top_artists_sorted = sorted(top_artists.items(), key=lambda x: -x[1])[:15]
    genre_sorted = sorted(genre_counts.items(), key=lambda x: -x[1])
    source_sorted = sorted(source_counts.items(), key=lambda x: -x[1])
    type_sorted = sorted(track_type_counts.items(), key=lambda x: -x[1])

    return render_template("stats.html",
                           total_playlists=total_playlists,
                           total_artists=len(all_artists),
                           total_tracks=len(all_uris),
                           genre_data=genre_sorted,
                           top_artists=top_artists_sorted,
                           source_data=source_sorted,
                           type_data=type_sorted)
