import csv
import os
import time
import random
import requests
from dotenv import load_dotenv

load_dotenv()

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_API_URL = "http://ws.audioscrobbler.com/2.0/"

# ─── Load Liked Songs CSV ─────────────────────────────────────────────────────

def load_liked_songs(path="Liked_Songs.csv"):
    liked_artists = set()
    liked_tracks = set()

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = row["Artist Name(s)"].replace(";", ",")
            artists = [a.strip().lower() for a in raw.split(",")]
            track = row["Track Name"].strip().lower()
            for artist in artists:
                liked_artists.add(artist)
                liked_tracks.add(f"{artist} - {track}")

    print(f"Loaded {len(liked_tracks)} liked tracks from {len(liked_artists)} artists")
    return liked_artists, liked_tracks

# ─── Last.fm: Find Similar Artists ───────────────────────────────────────────

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

def get_similar_artists(artist_name, limit=25):
    data = lastfm_get({"method": "artist.getSimilar", "artist": artist_name, "limit": limit})
    return [a["name"] for a in data.get("similarartists", {}).get("artist", [])]

def find_new_artists(liked_artists, target=200, sample_size=50):
    print(f"\nFinding {target} new artists via Last.fm (sampling {sample_size} of yours)...")
    sampled = random.sample(sorted(liked_artists), min(sample_size, len(liked_artists)))

    new_artists = {}
    for artist in sampled:
        similar = get_similar_artists(artist, limit=25)
        for s in similar:
            if s.lower() not in liked_artists:
                new_artists[s] = new_artists.get(s, 0) + 1
        time.sleep(0.2)

    ranked = sorted(new_artists.items(), key=lambda x: x[1], reverse=True)
    print(f"Found {len(ranked)} candidate artists, taking top {target}")
    return ranked[:target]

# ─── Last.fm: Top Tracks per Artist ──────────────────────────────────────────

def get_top_tracks(artist_name, count=3):
    """
    Gets tracks ranked 2-4 by Last.fm playcount - skips the #1 mega-hit
    but stays in well-known territory. Adjust offset to go deeper.
    """
    data = lastfm_get({"method": "artist.getTopTracks", "artist": artist_name, "limit": 10})
    tracks = data.get("toptracks", {}).get("track", [])
    # Skip rank 1 (the biggest hit), take next `count` tracks
    # This gives a nice mix - known but not overplayed
    selected = tracks[1:count+1] if len(tracks) > count else tracks[:count]
    return [t["name"] for t in selected]

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("Spotify Discovery Tool\n")

    liked_artists, liked_tracks = load_liked_songs("Liked_Songs.csv")
    new_artists = find_new_artists(liked_artists, target=200, sample_size=50)

    tracks_per_artist = 3
    print(f"\nFetching top {tracks_per_artist} tracks for each of {len(new_artists)} artists...")

    discoveries = []
    for i, (artist_name, score) in enumerate(new_artists, 1):
        tracks = get_top_tracks(artist_name, count=tracks_per_artist)
        new = []
        for t in tracks:
            key = f"{artist_name.lower()} - {t.lower()}"
            if key not in liked_tracks:
                new.append({"artist": artist_name, "track": t, "score": score, "key": key})
        discoveries.extend(new)
        if i % 25 == 0:
            print(f"  {i}/{len(new_artists)} done ({len(discoveries)} tracks so far)...")
        time.sleep(0.2)

    print(f"\nDone! {len(discoveries)} tracks from {len(new_artists)} artists")

    output = "discoveries.csv"
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["artist", "track", "score", "key"])
        writer.writeheader()
        for d in discoveries:
            writer.writerow(d)

    print(f"Saved to {output}")
    print(f"Run create_playlist.py to build your Spotify playlist!")

if __name__ == "__main__":
    main()
