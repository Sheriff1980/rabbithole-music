import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///rabbithole.db")

# For SQLite use check_same_thread=False
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)

def init_db():
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                avatar_url TEXT,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                token_expires_at INTEGER NOT NULL,
                liked_cache TEXT,
                liked_cache_at INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY,
                owner_id TEXT REFERENCES users(id),
                spotify_playlist_id TEXT,
                title TEXT NOT NULL,
                seed_type TEXT NOT NULL,
                seed_name TEXT,
                is_published INTEGER DEFAULT 0,
                published_at TIMESTAMP,
                track_data TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                progress INTEGER DEFAULT 0,
                progress_msg TEXT DEFAULT 'Starting...',
                excluded_genres TEXT DEFAULT '',
                playlist_size INTEGER DEFAULT 60,
                adventurousness INTEGER DEFAULT 3,
                genre_spread INTEGER DEFAULT 3,
                upvotes INTEGER DEFAULT 0,
                downvotes INTEGER DEFAULT 0,
                auto_refresh INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS playlist_votes (
                user_id TEXT,
                playlist_id TEXT,
                vote INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, playlist_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS track_votes (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                playlist_id TEXT,
                track_uri TEXT NOT NULL,
                vote INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, playlist_id, track_uri)
            )
        """))
        # Per-user thumbs up / down on individual tracks (result page)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS track_feedback (
                user_id TEXT NOT NULL,
                artist  TEXT NOT NULL,
                track_name TEXT NOT NULL,
                feedback INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, artist, track_name)
            )
        """))
        # Featured artists queue
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS featured_artists (
                id TEXT PRIMARY KEY,
                artist_name TEXT NOT NULL,
                spotify_artist_id TEXT,
                spotify_uri TEXT,
                image_url TEXT,
                bio TEXT,
                songs TEXT NOT NULL DEFAULT '[]',
                is_active INTEGER DEFAULT 0,
                queue_position INTEGER DEFAULT 0,
                activated_at TIMESTAMP,
                added_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # Artist self-submissions
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS featured_submissions (
                id TEXT PRIMARY KEY,
                artist_name TEXT NOT NULL,
                spotify_url TEXT,
                contact_email TEXT,
                message TEXT,
                status TEXT DEFAULT 'pending',
                reviewed_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # App-wide settings (k/v store)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """))
        conn.commit()

        # Migration: add deep_cuts, surprise_me, covers columns if missing
        for col in ("deep_cuts", "surprise_me", "covers"):
            try:
                conn.execute(text(f"ALTER TABLE playlists ADD COLUMN {col} INTEGER DEFAULT 0"))
                conn.commit()
            except Exception:
                conn.rollback()

def get_conn():
    return engine.connect()
