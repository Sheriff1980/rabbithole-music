"""
Discovery engine - adapted from discover.py for web use.
Accepts artist list directly instead of reading from CSV.
"""
import os
import time
import random
import requests
from dotenv import load_dotenv

load_dotenv()
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_API_URL = "http://ws.audioscrobbler.com/2.0/"

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

def get_similar_artists(artist_name, limit=20):
    data = lastfm_get({"method": "artist.getSimilar", "artist": artist_name, "limit": limit})
    return [a["name"] for a in data.get("similarartists", {}).get("artist", [])]

def get_top_tracks(artist_name, count=3):
    """Gets tracks ranked 2-4 - skips the #1 mega-hit, stays in well-known territory."""
    data = lastfm_get({"method": "artist.getTopTracks", "artist": artist_name, "limit": 10})
    tracks = data.get("toptracks", {}).get("track", [])
    selected = tracks[1:count+1] if len(tracks) > count else tracks[:count]
    return [t["name"] for t in selected]

def run_discovery(liked_artists, liked_track_keys, target_artists=40, sample_size=15, tracks_per_artist=3):
    """
    Main discovery function for web use.
    liked_artists: set of artist name strings (lowercase)
    liked_track_keys: set of "artist - track" strings (lowercase) to exclude
    Returns list of {artist, track} dicts
    """
    # Sample from liked artists
    sampled = random.sample(sorted(liked_artists), min(sample_size, len(liked_artists)))

    # Find similar artists
    new_artists = {}
    for artist in sampled:
        similar = get_similar_artists(artist, limit=20)
        for s in similar:
            if s.lower() not in liked_artists:
                new_artists[s] = new_artists.get(s, 0) + 1
        time.sleep(0.15)

    ranked = sorted(new_artists.items(), key=lambda x: x[1], reverse=True)[:target_artists]

    # Get tracks for each new artist
    discoveries = []
    for artist_name, score in ranked:
        tracks = get_top_tracks(artist_name, count=tracks_per_artist)
        for t in tracks:
            key = f"{artist_name.lower()} - {t.lower()}"
            if key not in liked_track_keys:
                discoveries.append({"artist": artist_name, "track": t})
        time.sleep(0.15)

    random.shuffle(discoveries)
    return discoveries
