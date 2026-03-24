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

def classify_genre(tags):
    for family, keywords in GENRE_FAMILIES.items():
        for tag in tags:
            if any(kw in tag for kw in keywords):
                return family
    return "other"

def get_top_tracks(artist_name, count=3):
    """Skip rank 1 (the mega-hit), take next `count` tracks."""
    data = lastfm_get({"method": "artist.getTopTracks", "artist": artist_name, "limit": 10})
    tracks = data.get("toptracks", {}).get("track", [])
    selected = tracks[1:count+1] if len(tracks) > count else tracks[:count]
    return [t["name"] for t in selected]

def run_discovery(liked_artists, liked_track_keys, target_artists=30, sample_size=25,
                  similar_limit=10, tracks_per_artist=3, excluded_genres=None, max_per_genre=3,
                  voted_liked=None, voted_disliked=None):
    """
    Breadth-first discovery:
    - Sample widely from the user's library
    - Take only 1-2 suggestions per seed artist (prevents one genre dominating)
    - Apply genre caps for diversity
    - Boost artists the user has liked before (extra seeds, higher priority)
    - Block artists the user has disliked before (never appear again)
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

    # Breadth-first: take at most 2 suggestions from each seed artist
    candidates = []  # ordered list preserving per-seed variety
    seen_candidates = set()

    for artist in sampled:
        similar = get_similar_artists(artist, limit=similar_limit)
        added = 0
        for s in similar:
            s_lower = s.lower()
            # Skip artists already in library, already seen, or previously disliked
            if s_lower in liked_artists or s_lower in seen_candidates:
                continue
            if s_lower in voted_disliked:
                continue
            candidates.append(s)
            seen_candidates.add(s_lower)
            added += 1
            if added >= 2:  # max 2 per seed = stays diverse
                break
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
        tracks = get_top_tracks(artist_name, count=tracks_per_artist)
        for t in tracks:
            key = f"{artist_name.lower()} - {t.lower()}"
            if key not in liked_track_keys:
                discoveries.append({"artist": artist_name, "track": t})
        time.sleep(0.15)

    random.shuffle(discoveries)
    return discoveries
