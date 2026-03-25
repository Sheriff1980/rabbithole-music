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

_VARIANT_WORDS = (
    r'remaster(ed)?|live(\s*(at|in|from)\s+\S.*)?'
    r'|single(\s*version)?|radio\s*(edit|version|mix)|album\s*version'
    r'|mono|stereo|\d{4}\s*(remaster|version|mix)'
    r'|original\s*(mix|version)'
    r'|extended(\s*(version|mix|cut))?'
    r'|acoustic(\s*(version|mix))?'
    r'|deluxe(\s*(edition|version))?'
    r'|bonus\s*track'
    r'|clean(\s*version)?|explicit(\s*version)?'
    r'|demo(\s*version)?'
    r'|instrumental(\s*(version|mix))?'
    r'|remix|re-?mix'
    r'|unplugged|stripped'
    r'|sped\s*up|slowed(\s*(down|\+\s*reverb))?'
    r'|from\s+["\u201c].*'
    r'|feat\.?\s.*|ft\.?\s.*|featuring\s.*'
)

# Precompiled patterns for performance
_RE_VARIANT_SUFFIX = re.compile(
    r'\s*[-–—]\s*(' + _VARIANT_WORDS + r')[^)}\]]*$', re.IGNORECASE
)
_RE_VARIANT_PAREN = re.compile(
    r'\s*[(\[]\s*(' + _VARIANT_WORDS + r')[^)}\]]*[)\]]', re.IGNORECASE
)
# Standalone variant word as a prefix: "Extended - You Right"
_RE_VARIANT_PREFIX = re.compile(
    r'^(' + _VARIANT_WORDS + r')\s*[-–—]\s*', re.IGNORECASE
)
_RE_TRAILING_JUNK = re.compile(r'[\s\-–—.,!?;:]+$')
_RE_MULTI_SPACE = re.compile(r'\s{2,}')
_RE_BARE_YEAR = re.compile(r'\s*[-–(]\s*\d{4}\s*\)?$')
# Normalize repeated characters: "haaa" → "ha", "ooooh" → "oh"
_RE_REPEATED_CHARS = re.compile(r'(.)\1{2,}')
# Strip all non-alphanumeric (keep spaces for word boundaries)
_RE_NON_ALNUM = re.compile(r'[^a-z0-9\s]')
# Normalize smart quotes / curly apostrophes to straight ones before stripping
_QUOTE_MAP = str.maketrans({
    '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
    '\u2013': '-', '\u2014': '-', '\u00b4': "'", '\u0060': "'",
})


def _normalize_track_name(name):
    """
    Aggressively normalize a track name for dedup.
    Strips: variant prefixes/suffixes, parenthetical tags, brackets,
    punctuation, extra spaces, trailing years, featured credits,
    and normalizes Unicode quotes/dashes and repeated characters.
    """
    name = name.lower().strip()

    # Normalize Unicode quotes/dashes to ASCII equivalents
    name = name.translate(_QUOTE_MAP)

    # Strip prefix variants: "Extended - You Right" → "You Right"
    name = _RE_VARIANT_PREFIX.sub('', name)

    # Strip suffix variants: "You Right - Extended" → "You Right"
    name = _RE_VARIANT_SUFFIX.sub('', name)

    # Strip parenthetical/bracket variants: "You Right (Deluxe)" → "You Right"
    name = _RE_VARIANT_PAREN.sub('', name)

    # Strip bare trailing year: "Song - 2024" or "Song (2024)"
    name = _RE_BARE_YEAR.sub('', name)

    # Remove ALL non-alphanumeric characters (punctuation, hyphens, apostrophes)
    # This catches: "ha-haaa!" vs "ha haaa", "don't" vs "dont", etc.
    name = _RE_NON_ALNUM.sub('', name)

    # Collapse repeated characters: "haaa" → "ha", "yeahhh" → "yeah"
    name = _RE_REPEATED_CHARS.sub(r'\1\1', name)

    # Collapse multiple spaces
    name = _RE_MULTI_SPACE.sub(' ', name)

    return name.strip()


def _is_track_duplicate(norm_new, existing_norms):
    """
    Check if a normalized track name is a duplicate of any existing one.
    Uses exact match first, then a startswith/endswith containment check
    for edge cases the normalizer missed.
    Returns the matching key if duplicate, None otherwise.
    """
    # Exact match first (covers 99% of cases after normalization)
    if norm_new in existing_norms:
        return norm_new

    # Containment safety net for things the normalizer didn't catch.
    # Only triggers if one name STARTS or ENDS with the other, AND
    # the leftover part is at most 2 short words (likely an unrecognized tag).
    if len(norm_new) >= 5:
        for existing in existing_norms:
            if len(existing) < 5:
                continue
            shorter, longer = (norm_new, existing) if len(norm_new) <= len(existing) else (existing, norm_new)

            # Must start or end with the shorter name
            if not (longer.startswith(shorter) or longer.endswith(shorter)):
                continue

            # Check that the leftover is short — likely a missed variant tag,
            # not a genuinely different song title
            if longer.startswith(shorter):
                leftover = longer[len(shorter):].strip(' -')
            else:
                leftover = longer[:len(longer)-len(shorter)].strip(' -')

            leftover_words = leftover.split()
            # At most 2 leftover words, and shorter name must be >= 2 words
            # (avoids "Stay" matching "Stay With Me" but catches
            #  "You Right" matching "You Right Special Cut")
            if len(leftover_words) <= 2 and len(shorter.split()) >= 2:
                return existing

    return None

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
            if not _is_track_duplicate(norm, seen):
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
                if not _is_track_duplicate(norm, seen):
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

def search_covers(sp, artist_name, playlist_size=60, progress_fn=None):
    """
    Find cover versions of an artist's songs performed by OTHER artists.
    1. Get the seed artist's top tracks from Spotify
    2. For each track, search Spotify for '[track name] cover'
    3. Filter out the original artist
    4. Return a list of cover track dicts with provenance
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        # Get the seed artist's top tracks
        results = sp.search(q=f"artist:{artist_name}", type="track", limit=30, market="US")
        items = results["tracks"]["items"]

        # Filter to tracks by the actual artist and dedup
        artist_lower = artist_name.lower()
        seed_tracks = []
        seen_names = set()
        for t in items:
            main = t["artists"][0]["name"].lower()
            if artist_lower not in main and main not in artist_lower:
                continue
            norm = _normalize_track_name(t["name"])
            if norm not in seen_names:
                seen_names.add(norm)
                seed_tracks.append({"name": t["name"], "artist": t["artists"][0]["name"]})
            if len(seed_tracks) >= 20:
                break

        if not seed_tracks:
            logger.warning("Covers: no tracks found for %s", artist_name)
            return []

        if progress_fn:
            progress_fn(35, f"Found {len(seed_tracks)} songs by {artist_name}, searching for covers...")

        # Search for covers of each track
        found = []
        seen_uris = set()
        total = len(seed_tracks)

        for i, seed in enumerate(seed_tracks):
            if len(found) >= playlist_size:
                break

            # Search strategies — try multiple queries for better results
            queries = [
                f'"{seed["name"]}" cover',
                f'{seed["name"]} cover',
            ]

            for q in queries:
                if len(found) >= playlist_size:
                    break
                try:
                    cover_results = sp.search(q=q, type="track", limit=20, market="US")
                    cover_items = cover_results["tracks"]["items"]
                except Exception:
                    continue

                for t in cover_items:
                    if len(found) >= playlist_size:
                        break

                    cover_artist = t["artists"][0]["name"].lower()

                    # Skip the original artist
                    if artist_lower in cover_artist or cover_artist in artist_lower:
                        continue

                    uri = t["uri"]
                    if uri in seen_uris:
                        continue
                    seen_uris.add(uri)

                    # Check the track name actually resembles the original
                    cover_norm = _normalize_track_name(t["name"])
                    seed_norm = _normalize_track_name(seed["name"])
                    # The cover should contain the original song name (or vice versa)
                    if seed_norm not in cover_norm and cover_norm not in seed_norm:
                        # Looser check — at least 60% of words match
                        seed_words = set(seed_norm.split())
                        cover_words = set(cover_norm.split())
                        if len(seed_words) > 0:
                            overlap = len(seed_words & cover_words) / len(seed_words)
                            if overlap < 0.6:
                                continue

                    found.append({
                        "uri": uri,
                        "name": t["name"],
                        "artist": t["artists"][0]["name"],
                        "album": t["album"]["name"],
                        "album_art": t["album"]["images"][1]["url"] if len(t["album"]["images"]) > 1 else None,
                        "preview_url": t.get("preview_url"),
                        "popularity": t.get("popularity", 50),
                        "provenance": {
                            "seed_artist": artist_name,
                            "degree": 0,
                            "source": "cover",
                            "genre": "",
                            "tags": [],
                            "track_type": "cover",
                            "original_song": seed["name"],
                        }
                    })

            if progress_fn and total > 0:
                pct = 35 + int(55 * (i + 1) / total)
                if i % 3 == 2 or i == total - 1:
                    progress_fn(pct, f"Found {len(found)} covers ({i+1}/{total} songs searched)...")

            time.sleep(0.15)  # Rate limit

        return found

    except Exception:
        logger.exception("Cover search failed for %s", artist_name)
        return []


def search_random_covers(sp, playlist_size=60, progress_fn=None):
    """
    Surprise covers — pick random well-known artists and find covers of their songs.
    Creates a grab bag of covers spanning multiple genres.
    """
    import logging
    import random
    logger = logging.getLogger(__name__)

    # Diverse pool of well-known artists people love to cover
    COVERABLE_ARTISTS = [
        "The Beatles", "Fleetwood Mac", "Queen", "David Bowie", "Prince",
        "Nirvana", "Radiohead", "Led Zeppelin", "Johnny Cash", "Dolly Parton",
        "Whitney Houston", "Aretha Franklin", "Michael Jackson", "Stevie Wonder",
        "Bob Dylan", "The Rolling Stones", "Elton John", "Adele", "Amy Winehouse",
        "Coldplay", "Foo Fighters", "Red Hot Chili Peppers", "U2", "Metallica",
        "Eagles", "Pink Floyd", "Bon Jovi", "AC/DC", "Guns N' Roses",
        "Tina Turner", "Cher", "Madonna", "Cyndi Lauper", "Blondie",
        "Leonard Cohen", "Simon & Garfunkel", "Carole King", "James Taylor",
        "Billie Holiday", "Frank Sinatra", "Ella Fitzgerald", "Ray Charles",
        "Otis Redding", "Sam Cooke", "Marvin Gaye", "Al Green",
        "Talking Heads", "The Cure", "Depeche Mode", "Joy Division",
        "Pixies", "R.E.M.", "The Smiths", "Oasis",
        "Taylor Swift", "Billie Eilish", "Lana Del Rey", "Hozier",
    ]

    random.shuffle(COVERABLE_ARTISTS)

    # Pick 8-12 random artists to pull covers from
    num_artists = min(12, max(8, playlist_size // 5))
    selected_artists = COVERABLE_ARTISTS[:num_artists]

    if progress_fn:
        progress_fn(15, f"Picking {num_artists} legendary artists to find covers of...")

    all_found = []
    all_seen_uris = set()
    per_artist_target = max(5, playlist_size // num_artists + 2)

    for idx, artist_name in enumerate(selected_artists):
        if len(all_found) >= playlist_size:
            break

        if progress_fn:
            pct = 15 + int(75 * (idx + 1) / num_artists)
            progress_fn(pct, f"Finding covers of {artist_name}...")

        covers = search_covers(sp, artist_name, playlist_size=per_artist_target)
        # Cross-batch URI dedup
        for c in covers:
            uri = c.get("uri", "")
            if uri and uri not in all_seen_uris:
                all_seen_uris.add(uri)
                all_found.append(c)

    # Shuffle so it's not clustered by original artist
    random.shuffle(all_found)
    return all_found[:playlist_size]


def dedup_playlist(tracks, covers_mode=False):
    """
    Final dedup pass on the assembled playlist. Three layers:
    1. URI dedup — same Spotify URI = same track, instant kill
    2. Per-artist name dedup — same artist + similar track name
    3. Cross-artist name dedup — same normalized track name regardless of artist
       (catches covers, re-releases under different artist entries)
       SKIPPED in covers_mode since different artists covering the same song is the point.
    Prefers the version with higher popularity when dupes are found.
    """
    seen_uris   = {}   # uri -> index in result list
    artist_seen = {}   # artist_lower -> set of normalized names
    artist_idx  = {}   # "artist::norm" -> index in result list
    global_seen = {}   # norm_name -> index in result list (cross-artist)
    result = []

    for track in tracks:
        uri = track.get("uri", "")
        norm_name = _normalize_track_name(track.get("name", ""))
        norm_artist = track.get("artist", "").lower().strip()

        # Layer 1: URI dedup — identical Spotify track
        if uri and uri in seen_uris:
            continue

        if norm_artist not in artist_seen:
            artist_seen[norm_artist] = set()

        # Layer 2: Per-artist name dedup
        dup_key = _is_track_duplicate(norm_name, artist_seen[norm_artist])

        if dup_key is not None:
            lookup = f"{norm_artist}::{dup_key}"
            if lookup in artist_idx:
                existing_idx = artist_idx[lookup]
                existing_pop = result[existing_idx].get("popularity", 50)
                new_pop = track.get("popularity", 50)
                if new_pop > existing_pop:
                    result[existing_idx] = track
            continue

        # Layer 3: Cross-artist dedup — same song name from different artists
        # Skip in covers mode: different artists covering the same song is intentional
        if not covers_mode and norm_name in global_seen:
            existing_idx = global_seen[norm_name]
            existing_pop = result[existing_idx].get("popularity", 50)
            new_pop = track.get("popularity", 50)
            if new_pop > existing_pop:
                result[existing_idx] = track
            continue

        # Track is unique — add it
        if uri:
            seen_uris[uri] = len(result)
        artist_seen[norm_artist].add(norm_name)
        artist_idx[f"{norm_artist}::{norm_name}"] = len(result)
        global_seen[norm_name] = len(result)
        result.append(track)

    return result


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
