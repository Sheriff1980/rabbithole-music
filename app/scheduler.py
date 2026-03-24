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
    _scheduler.start()
    logger.info("Scheduler started — weekly refresh fires every Monday at 08:00 CT.")
