"""
Weekly auto-refresh scheduler.
Runs every Monday at 08:00 and kicks off a new generation for every playlist
that has auto_refresh=1, using the same seed + settings as the original.

Only starts in the main Flask worker process (not the Werkzeug reloader watchdog)
so jobs don't fire twice in debug mode.
"""
import os
import uuid
import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

logger = logging.getLogger(__name__)
_scheduler = None


def weekly_refresh(app):
    """Find all auto_refresh playlists and kick off new generation jobs."""
    from .models import engine, get_conn
    from .routes.generate import run_discovery_background
    from .utils import decrypt_token

    logger.info("Weekly auto-refresh: starting...")

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.id, p.owner_id, p.seed_type, p.seed_name,
                   p.excluded_genres, p.playlist_size, p.adventurousness, p.genre_spread,
                   u.access_token
            FROM playlists p
            JOIN users u ON p.owner_id = u.id
            WHERE p.auto_refresh = 1 AND p.status = 'done'
        """)).fetchall()

    if not rows:
        logger.info("Weekly auto-refresh: no playlists to refresh.")
        return

    logger.info("Weekly auto-refresh: refreshing %d playlist(s).", len(rows))

    with app.app_context():
        with engine.connect() as conn:
            for row in rows:
                (src_id, owner_id, seed_type, seed_name,
                 excl, psize, adv, spread, enc_token) = row

                new_id    = str(uuid.uuid4())
                new_title = f"Rabbithole - {seed_name} (Weekly)"
                excl_list = [g for g in (excl or "").split(",") if g]

                conn.execute(text("""
                    INSERT INTO playlists
                      (id, owner_id, title, seed_type, seed_name, track_data, status,
                       excluded_genres, playlist_size, adventurousness, genre_spread)
                    VALUES (:id, :owner, :title, :stype, :sname, '[]', 'pending',
                            :excl, :psize, :adv, :spread)
                """), {
                    "id": new_id, "owner": owner_id, "title": new_title,
                    "stype": seed_type, "sname": seed_name,
                    "excl": excl, "psize": psize, "adv": adv, "spread": spread,
                })

                t = threading.Thread(
                    target=run_discovery_background,
                    args=(new_id, owner_id, seed_type, decrypt_token(enc_token),
                          excl_list, psize, adv, spread),
                    daemon=True,
                )
                t.start()
                logger.info("Auto-refresh: kicked off job %s for user %s", new_id, owner_id)

            conn.commit()


def rotate_featured_artist(app):
    """
    Auto-rotate the featured artist to the next one in the queue.
    Reads rotation interval from app_settings (key='featured_rotation_hours', default 24).
    Only rotates if there are 2+ featured artists in the queue.
    """
    from .models import get_conn

    logger.info("Featured artist rotation: checking...")

    try:
        with get_conn() as conn:
            # Get rotation interval from settings (default: 24 hours)
            row = conn.execute(text(
                "SELECT value FROM app_settings WHERE key='featured_rotation_hours'"
            )).fetchone()
            # Don't rotate if set to 0 (manual mode)
            interval_hours = int(row[0]) if row else 24
            if interval_hours <= 0:
                logger.info("Featured rotation disabled (interval=0).")
                return

            # Count total artists
            count = conn.execute(text(
                "SELECT COUNT(*) FROM featured_artists"
            )).fetchone()[0]
            if count < 2:
                logger.info("Featured rotation: fewer than 2 artists, skipping.")
                return

            # Find current active artist
            active = conn.execute(text(
                "SELECT id, queue_position FROM featured_artists WHERE is_active=1 LIMIT 1"
            )).fetchone()

            if active:
                current_pos = active[1]
                # Find the next artist in queue order (wrap around)
                next_artist = conn.execute(text(
                    "SELECT id FROM featured_artists WHERE queue_position > :pos ORDER BY queue_position ASC LIMIT 1"
                ), {"pos": current_pos}).fetchone()

                if not next_artist:
                    # Wrap to beginning
                    next_artist = conn.execute(text(
                        "SELECT id FROM featured_artists ORDER BY queue_position ASC LIMIT 1"
                    )).fetchone()
            else:
                # No active artist — activate the first one
                next_artist = conn.execute(text(
                    "SELECT id FROM featured_artists ORDER BY queue_position ASC LIMIT 1"
                )).fetchone()

            if next_artist:
                conn.execute(text("UPDATE featured_artists SET is_active=0"))
                conn.execute(text(
                    "UPDATE featured_artists SET is_active=1, activated_at=CURRENT_TIMESTAMP WHERE id=:id"
                ), {"id": next_artist[0]})
                conn.commit()
                logger.info("Featured rotation: activated artist %s", next_artist[0])

    except Exception as e:
        logger.error("Featured rotation error: %s", e)


def start_scheduler(app):
    """
    Start the APScheduler background scheduler.
    Safe to call multiple times — only one scheduler is ever created.
    - Flask dev mode: skips the Werkzeug watchdog process (avoids double-firing)
    - Gunicorn / Railway: always starts (single worker, no reloader)
    """
    global _scheduler

    if _scheduler is not None:
        return

    # In Flask debug mode, Werkzeug spawns two processes.
    # Only run in the child (the actual serving process).
    flask_debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    is_werkzeug_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    if flask_debug and not is_werkzeug_child:
        return

    _scheduler = BackgroundScheduler(timezone="America/Chicago")
    _scheduler.add_job(
        func=lambda: weekly_refresh(app),
        trigger=CronTrigger(day_of_week="mon", hour=8, minute=0),
        id="weekly_refresh",
        name="Weekly playlist auto-refresh",
        replace_existing=True,
    )
    _scheduler.add_job(
        func=lambda: rotate_featured_artist(app),
        trigger=CronTrigger(hour="*/6"),  # Check every 6 hours
        id="featured_rotation",
        name="Featured artist auto-rotation",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started — weekly refresh Mon 08:00 CT, featured rotation every 6h.")
