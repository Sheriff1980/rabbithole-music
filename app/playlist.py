"""Spotify playlist operations."""
import json
import time

CACHE_TTL = 60 * 60 * 6  # 6 hours

def _parse_tracks(items):
    artists = set()
    tracks = set()
    for item in items:
        track = item.get("track")
        if not track:
            continue
        track_name = track["name"].strip().lower()
        for artist in track.get("artists", []):
            artist_name = artist["name"].strip().lower()
            artists.add(artist_name)
            tracks.add(f"{artist_name} - {track_name}")
    return artists, tracks

def get_liked_artists_and_tracks(sp, user_id=None, engine=None):
    """Fetch liked songs with DB caching. Falls back to live fetch if no cache."""
    import time as t
    now = int(t.time())

    # Try cache first
    if user_id and engine:
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT liked_cache, liked_cache_at FROM users WHERE id=:id"),
                {"id": user_id}
            ).fetchone()
        if row and row[0] and row[1] and (now - row[1]) < CACHE_TTL:
            data = json.loads(row[0])
            return set(data["artists"]), set(data["tracks"])

    # Live fetch
    liked_artists = set()
    liked_tracks = set()
    offset = 0
    while True:
        results = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = results.get("items", [])
        if not items:
            break
        a, t2 = _parse_tracks(items)
        liked_artists |= a
        liked_tracks |= t2
        offset += 50
        if not results.get("next"):
            break

    # Save to cache
    if user_id and engine:
        from sqlalchemy import text
        cache_data = json.dumps({"artists": list(liked_artists), "tracks": list(liked_tracks)})
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE users SET liked_cache=:c, liked_cache_at=:ts WHERE id=:id"),
                {"c": cache_data, "ts": now, "id": user_id}
            )
            conn.commit()

    return liked_artists, liked_tracks

def get_playlist_artists_and_tracks(sp, playlist_id):
    """Fetch tracks from a specific playlist."""
    liked_artists = set()
    liked_tracks = set()
    offset = 0
    while True:
        results = sp.playlist_items(playlist_id, limit=100, offset=offset)
        items = results.get("items", [])
        if not items:
            break
        a, t = _parse_tracks(items)
        liked_artists |= a
        liked_tracks |= t
        offset += 100
        if not results.get("next"):
            break
    return liked_artists, liked_tracks

import re

def _normalize_track_name(name):
    """Strip remaster/live/version/edit suffixes for deduplication."""
    name = name.lower().strip()
    name = re.sub(r'\s*[-–(]\s*(remaster(ed)?|live|single version|radio edit|album version'
                  r'|mono|stereo|\d{4} remaster|\d{4} version|original mix)[^)]*\)?.*$',
                  '', name, flags=re.IGNORECASE)
    return name.strip()

def search_artist_tracks(sp, artist_name, count=3, deep_cuts=False):
    """
    Search for tracks by artist in one API call.
    Deduplicates by normalized track name to avoid remaster/live duplicates.
    If deep_cuts=True, filters to tracks with Spotify popularity < 40.
    Returns up to `count` track dicts.
    """
    try:
        # Fetch more results when filtering for deep cuts
        fetch_limit = min(50, count * 10) if deep_cuts else count * 4
        results = sp.search(q=f"artist:{artist_name}", type="track", limit=fetch_limit, market="US")
        items = results["tracks"]["items"]
        seen = set()
        found = []
        artist_lower = artist_name.lower()

        # Deep cuts: sort by popularity ascending so we prefer obscure tracks
        if deep_cuts:
            items = sorted(items, key=lambda t: t.get("popularity", 50))

        popularity_threshold = 40

        for t in items:
            main_artist = t["artists"][0]["name"].lower()
            if artist_lower not in main_artist and main_artist not in artist_lower:
                continue

            # Deep cuts: skip popular tracks
            if deep_cuts and t.get("popularity", 50) > popularity_threshold:
                continue

            norm = _normalize_track_name(t["name"])
            if norm not in seen:
                seen.add(norm)
                found.append({
                    "uri": t["uri"],
                    "name": t["name"],
                    "artist": t["artists"][0]["name"],
                    "album": t["album"]["name"],
                    "album_art": t["album"]["images"][1]["url"] if len(t["album"]["images"]) > 1 else None,
                    "preview_url": t.get("preview_url"),
                    "popularity": t.get("popularity", 50),
                })
            if len(found) >= count:
                break

        # If deep cuts didn't find enough, relax threshold to 50
        if deep_cuts and len(found) < count:
            for t in items:
                if len(found) >= count:
                    break
                main_artist = t["artists"][0]["name"].lower()
                if artist_lower not in main_artist and main_artist not in artist_lower:
                    continue
                if t.get("popularity", 50) > 50:
                    continue
                norm = _normalize_track_name(t["name"])
                if norm not in seen:
                    seen.add(norm)
                    found.append({
                        "uri": t["uri"],
                        "name": t["name"],
                        "artist": t["artists"][0]["name"],
                        "album": t["album"]["name"],
                        "album_art": t["album"]["images"][1]["url"] if len(t["album"]["images"]) > 1 else None,
                        "preview_url": t.get("preview_url"),
                        "popularity": t.get("popularity", 50),
                    })

        return found
    except Exception:
        return []

def search_track(sp, artist, track):
    try:
        results = sp.search(q=f"track:{track} artist:{artist}", type="track", limit=1, market="US")
        items = results["tracks"]["items"]
        if items:
            t = items[0]
            return {
                "uri": t["uri"],
                "name": t["name"],
                "artist": t["artists"][0]["name"],
                "album": t["album"]["name"],
                "album_art": t["album"]["images"][1]["url"] if len(t["album"]["images"]) > 1 else None,
                "preview_url": t.get("preview_url"),
            }
        return None
    except Exception:
        return None

def push_to_spotify(sp, user_id, tracks, playlist_name):
    """Create a playlist and add tracks. Returns spotify playlist URL."""
    playlist = sp.user_playlist_create(
        user=user_id,
        name=playlist_name,
        public=False,
        description="Generated by Rabbithole Music - discover music you didn't know you needed"
    )
    playlist_id = playlist["id"]
    uris = [t["uri"] for t in tracks if t.get("uri")]
    for i in range(0, len(uris), 100):
        sp.playlist_add_items(playlist_id, uris[i:i+100])
        time.sleep(0.3)
    return playlist["external_urls"]["spotify"], playlist_id
