"""
Shared security utilities:
  - CSRF token generation + request validation
  - Spotify token encryption / decryption (at-rest protection)
"""
import os
import secrets
import hashlib
import base64
import logging
from functools import wraps

from flask import session, request, jsonify
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


# ── CSRF ──────────────────────────────────────────────────────────────────────

def get_csrf_token():
    """Return the CSRF token for the current session, creating one if needed."""
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']


def csrf_required(f):
    """
    Decorator that enforces a CSRF token on all state-changing requests.
    Checks (in order):
      1. X-CSRFToken request header  (fetch / XHR calls)
      2. _csrf_token form field       (HTML form POSTs)
    GET / HEAD / OPTIONS are always allowed through.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return f(*args, **kwargs)

        token = (
            request.headers.get('X-CSRFToken') or
            request.form.get('_csrf_token')
        )
        expected = session.get('_csrf_token')

        if not token or not expected or token != expected:
            logger.warning(
                "CSRF check failed for %s %s — got %r, expected %r",
                request.method, request.path, token, expected
            )
            # Return JSON for XHR or plain 403 for form submissions
            if request.is_json or request.headers.get('X-CSRFToken'):
                return jsonify({'error': 'Invalid CSRF token'}), 403
            return 'Forbidden — invalid CSRF token', 403

        return f(*args, **kwargs)
    return decorated


# ── Token encryption ──────────────────────────────────────────────────────────

def _get_fernet() -> Fernet:
    """
    Build a Fernet cipher keyed from FLASK_SECRET_KEY.
    The secret is SHA-256 hashed to get exactly 32 bytes, then base64url-encoded
    for Fernet's expected key format.
    """
    secret = os.getenv('FLASK_SECRET_KEY', 'dev-secret-change-me')
    raw_key = hashlib.sha256(secret.encode()).digest()   # 32 bytes
    return Fernet(base64.urlsafe_b64encode(raw_key))


def encrypt_token(plaintext: str) -> str:
    """Encrypt a Spotify token string for storage in the DB."""
    if not plaintext:
        return plaintext
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(value: str) -> str:
    """
    Decrypt a Spotify token string read from the DB.
    Falls back to returning the raw value for tokens stored before encryption
    was introduced (so existing sessions survive the upgrade).
    """
    if not value:
        return value
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except (InvalidToken, Exception):
        # Pre-migration token — stored in plaintext
        return value


# ── Admin helpers ────────────────────────────────────────────────────────────

def is_admin():
    """Check if the current session user is an admin."""
    admin_ids = os.getenv("ADMIN_USER_IDS", "").split(",")
    return session.get("user_id") in [a.strip() for a in admin_ids if a.strip()]


def admin_required(f):
    """Decorator that restricts a route to admin users only."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin():
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated
