"""
Discovery engine - breadth-first approach for maximum genre diversity.
Takes a spread of suggestions from each seed artist rather than
ranking by frequency (which causes genre clustering).
"""
import os
import time
import random
import requests
from dotenv import load_dotenv

load_dotenv()
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_API_URL = "http://ws.audioscrobbler.com/2.0/"

GENRE_FAMILIES = {
    "jazz":        ["jazz", "smooth jazz", "jazz fusion", "bebop", "swing", "big band"],
    "classical":   ["classical", "orchestral", "opera", "chamber music", "baroque", "symphony"],
    "metal":       ["metal", "heavy metal", "death metal", "black metal", "doom metal", "thrash", "metalcore"],
    "hip-hop":     ["hip-hop", "hip hop", "rap", "trap", "gangsta rap"],
    "electronic":  ["electronic", "edm", "techno", "house", "trance", "dubstep", "drum and bass"],
    "country":     ["country", "country rock", "alt-country", "bluegrass", "honky tonk"],
    "r&b":         ["r&b", "soul", "funk", "neo soul", "motown", "rhythm and blues"],
    "folk":        ["folk", "indie folk", "folk rock", "acoustic folk"],
    "pop":         ["pop", "dance pop", "electropop", "synth-pop", "teen pop"],
    "rock":        ["rock", "alternative rock", "indie rock", "classic rock", "punk", "garage rock"],
    "ambient":     ["ambient", "new age", "drone", "meditation"],
    "reggae":      ["reggae", "ska", "dub", "dancehall"],
}

def lastfm_get(params):
    try:
        resp = requests.get(LASTFM_API_URL, params={
            **params,
            "api_key": LASTFM_API_KEY,
            "format": "json",
        }, timeout=10)
        if not resp.text.strip():
            return {}
        return resp.json()
    except Exception:
        return {}

def get_similar_artists(artist_name, limit=10):
    data = lastfm_get({"method": "artist.getSimilar", "artist": artist_name, "limit": limit})
    return [a["name"] for a in data.get("similarartists", {}).get("artist", [])]

def get_artist_tags(artist_name):
    data = lastfm_get({"method": "artist.getTopTags", "artist": artist_name})
    tags = data.get("toptags", {}).get("tag", [])
    return [t["name"].lower() for t in tags[:8]]

ADJACENT_GENRES = {
    "jazz":       ["r&b", "classical", "folk"],
    "classical":  ["jazz", "ambient", "folk"],
    "metal":      ["rock", "hip-hop"],
    "hip-hop":    ["r&b", "electronic", "metal"],
    "electronic": ["ambient", "pop", "hip-hop"],
    "country":    ["folk", "rock", "r&b"],
    "r&b":        ["hip-hop", "jazz", "pop"],
    "folk":       ["country", "rock", "jazz"],
    "pop":        ["electronic", "r&b", "rock"],
    "rock":       ["metal", "folk", "pop"],
    "ambient":    ["electronic", "classical", "jazz"],
    "reggae":     ["r&b", "hip-hop", "folk"],
}

def classify_genre(tags):
    for family, keywords in GENRE_FAMILIES.items():
        for tag in tags:
            if any(kw in tag for kw in keywords):
                return family
    return "other"

def get_top_tracks(artist_name, count=3, deep_cuts=False):
    """
    Skip rank 1 (the mega-hit), take next `count` tracks.
    If deep_cuts=True, skip top 5 and pull from positions 6+.
    """
    fetch_limit = 20 if deep_cuts else 10
    data = lastfm_get({"method": "artist.getTopTracks", "artist": artist_name, "limit": fetch_limit})
    tracks = data.get("toptracks", {}).get("track", [])
    if deep_cuts:
        # Skip the top 5 popular tracks, take from position 6 onward
        selected = tracks[5:5+count] if len(tracks) > 5 else tracks[-count:]
    else:
        selected = tracks[1:count+1] if len(tracks) > count else tracks[:count]
    return [t["name"] for t in selected]


def get_genre_seed_artists(genre_name, limit=20):
    """Fetch top artists for a genre tag from Last.fm."""
    data = lastfm_get({"method": "tag.getTopArtists", "tag": genre_name, "limit": limit})
    artists = data.get("topartists", {}).get("artist", [])
    return [a["name"].lower() for a in artists]

def run_discovery(liked_artists, liked_track_keys, target_artists=30, sample_size=25,
                  similar_limit=10, tracks_per_artist=3, excluded_genres=None, max_per_genre=3,
                  voted_liked=None, voted_disliked=None,
                  surprise_me=False, deep_cuts=False, progress_fn=None):
    """
    Breadth-first discovery:
    - Sample widely from the user's library
    - Take only 1-2 suggestions per seed artist (prevents one genre dominating)
    - Apply genre caps for diversity
    - Boost artists the user has liked before (extra seeds, higher priority)
    - Block artists the user has disliked before (never appear again)
    - surprise_me=True: 2nd-degree expansion + adjacent genre injection + random chart artists
    - deep_cuts=True: skip popular tracks, pull from positions 6+ on Last.fm
    - Result: suggestions that span the full width of your taste
    """
    excluded_genres  = [g.lower().strip() for g in (excluded_genres or [])]
    voted_liked      = voted_liked or set()
    voted_disliked   = voted_disliked or set()

    # Boost: treat thumbed-up artists as extra seeds at the front of the sample
    boosted = [a for a in sorted(liked_artists) if a.lower() in voted_liked]
    remaining = [a for a in sorted(liked_artists) if a.lower() not in voted_liked]
    random.shuffle(remaining)

    # Combine: boosted artists first, then random sample fills the rest
    pool = boosted + remaining
    sampled = pool[:sample_size]

    # ── Surprise Me: inject adjacent genre artists + random chart artists ───
    if surprise_me and sampled:
        # Get tags from the first few seed artists to find their genre families
        seed_genres = set()
        for artist in sampled[:5]:
            tags = get_artist_tags(artist)
            genre = classify_genre(tags)
            if genre != "other":
                seed_genres.add(genre)
            time.sleep(0.1)

        # Find adjacent genres and inject artists from them
        adjacent_artists = []
        for g in seed_genres:
            for adj in ADJACENT_GENRES.get(g, []):
                adj_artists = get_genre_seed_artists(adj, limit=10)
                adjacent_artists.extend(adj_artists)
                time.sleep(0.15)

        # Add unique adjacent artists to the sample pool
        sampled_lower = {a.lower() for a in sampled}
        for a in adjacent_artists:
            if a.lower() not in sampled_lower and a.lower() not in voted_disliked:
                sampled.append(a)
                sampled_lower.add(a.lower())

        # Inject 5 random chart artists for true unpredictability
        chart_data = lastfm_get({"method": "chart.getTopArtists", "limit": 50})
        chart_artists = chart_data.get("artists", {}).get("artist", [])
        if chart_artists:
            random.shuffle(chart_artists)
            injected = 0
            for ca in chart_artists:
                ca_name = ca["name"].lower()
                if ca_name not in sampled_lower and ca_name not in voted_disliked:
                    sampled.append(ca["name"])
                    sampled_lower.add(ca_name)
                    injected += 1
                    if injected >= 5:
                        break

    # Breadth-first: take at most 2 suggestions from each seed artist
    max_per_seed = 4 if surprise_me else 2
    candidates = []
    seen_candidates = set()

    for artist in sampled:
        similar = get_similar_artists(artist, limit=similar_limit)
        added = 0
        for s in similar:
            s_lower = s.lower()
            if s_lower in liked_artists or s_lower in seen_candidates:
                continue
            if s_lower in voted_disliked:
                continue
            candidates.append(s)
            seen_candidates.add(s_lower)
            added += 1
            if added >= max_per_seed:
                break
        time.sleep(0.15)

    # ── Surprise Me: 2nd-degree expansion ──────────────────────────────────
    if surprise_me:
        if progress_fn:
            progress_fn(45, "Going deeper... 2nd-degree artist expansion")
        # Take top 20 candidates and find THEIR similar artists too
        second_degree_seeds = candidates[:20]
        for artist in second_degree_seeds:
            similar = get_similar_artists(artist, limit=5)
            for s in similar:
                s_lower = s.lower()
                if s_lower not in seen_candidates and s_lower not in liked_artists:
                    if s_lower not in voted_disliked:
                        candidates.append(s)
                        seen_candidates.add(s_lower)
            time.sleep(0.15)

    # Shuffle so genre filtering doesn't always favour the same seed artists
    random.shuffle(candidates)

    # Apply genre diversity cap + exclusions
    genre_counts = {}
    filtered = []

    for artist_name in candidates:
        if len(filtered) >= target_artists:
            break

        # Hard block — user thumbed this artist down before
        if artist_name.lower() in voted_disliked:
            continue

        tags = get_artist_tags(artist_name)
        genre = classify_genre(tags)
        time.sleep(0.1)

        # Skip explicitly excluded genres
        if genre in excluded_genres:
            continue
        if any(ex in tag for ex in excluded_genres for tag in tags):
            continue

        # Cap per genre for diversity (default max 3 per genre family)
        if genre != "other" and genre_counts.get(genre, 0) >= max_per_genre:
            continue

        genre_counts[genre] = genre_counts.get(genre, 0) + 1
        filtered.append(artist_name)

    # Get tracks for each filtered artist
    discoveries = []
    for artist_name in filtered:
        tracks = get_top_tracks(artist_name, count=tracks_per_artist, deep_cuts=deep_cuts)
        for t in tracks:
            key = f"{artist_name.lower()} - {t.lower()}"
            if key not in liked_track_keys:
                discoveries.append({"artist": artist_name, "track": t})
        time.sleep(0.15)

    random.shuffle(discoveries)
    return discoveries
