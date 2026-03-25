import os
import logging
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")

    secret = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    if secret == "dev-secret-change-me":
        logger.warning(
            "FLASK_SECRET_KEY is not set — using insecure default. "
            "Set a strong random key in your .env before deploying."
        )
    app.secret_key = secret

    # ── Rate limiter ──────────────────────────────────────────────────────────
    from .limiter import limiter
    limiter.init_app(app)

    # ── Database ──────────────────────────────────────────────────────────────
    from .models import init_db
    init_db()

    # ── Blueprints ────────────────────────────────────────────────────────────
    from .routes.main import main_bp
    from .routes.generate import generate_bp
    from .routes.community import community_bp
    from .routes.feedback import feedback_bp
    from .routes.admin import admin_bp
    from .auth import auth_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(generate_bp, url_prefix="/generate")
    app.register_blueprint(community_bp, url_prefix="/community")
    app.register_blueprint(feedback_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(auth_bp, url_prefix="/auth")

    # ── Template globals ──────────────────────────────────────────────────────
    from .utils import get_csrf_token, is_admin
    app.jinja_env.globals['csrf_token'] = get_csrf_token
    app.jinja_env.globals['is_admin'] = is_admin

    # ── Featured artist context processor ────────────────────────────────────
    import json as _json
    import time as _time
    from sqlalchemy import text as _text

    _featured_cache = {"data": None, "ts": 0}

    @app.context_processor
    def inject_featured_artist():
        now = _time.time()
        # Cache for 60 seconds to avoid a DB query on every page load
        if _featured_cache["data"] is not None and (now - _featured_cache["ts"]) < 60:
            return _featured_cache["data"]

        from .models import get_conn
        try:
            with get_conn() as conn:
                row = conn.execute(_text(
                    "SELECT id, artist_name, image_url, bio, songs, spotify_uri "
                    "FROM featured_artists WHERE is_active=1 LIMIT 1"
                )).fetchone()
            if row:
                result = {"featured_artist": {
                    "id": row[0], "name": row[1], "image": row[2],
                    "bio": row[3], "songs": _json.loads(row[4] or "[]"),
                    "uri": row[5],
                }}
            else:
                result = {"featured_artist": None}
        except Exception:
            result = {"featured_artist": None}

        _featured_cache["data"] = result
        _featured_cache["ts"] = now
        return result

    # ── Weekly refresh scheduler ──────────────────────────────────────────────
    from .scheduler import start_scheduler
    start_scheduler(app)

    # ── Rate limit error handler ──────────────────────────────────────────────
    from flask import jsonify, request as flask_request
    from flask_limiter.errors import RateLimitExceeded

    @app.errorhandler(RateLimitExceeded)
    def handle_rate_limit(e):
        if flask_request.is_json or flask_request.headers.get('X-CSRFToken'):
            return jsonify({'error': 'Too many requests — slow down a little.'}), 429
        return '<h1>Too many requests</h1><p>Please wait a moment and try again.</p>', 429

    return app
