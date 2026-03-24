import os
import secrets
import time
import json
from flask import Blueprint, redirect, request, session, url_for
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import MemoryCacheHandler
from .models import get_conn
from .utils import encrypt_token, decrypt_token
from sqlalchemy import text

auth_bp = Blueprint("auth", __name__)

SCOPE = "user-library-read playlist-read-private playlist-modify-public playlist-modify-private"


def get_spotify_oauth(state=None):
    return SpotifyOAuth(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:5000/auth/callback"),
        scope=SCOPE,
        cache_handler=MemoryCacheHandler(),
        state=state,
        show_dialog=False,
    )


def get_spotify_client():
    """Get an authenticated Spotify client for the current session user."""
    if "user_id" not in session:
        return None
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT access_token, refresh_token, token_expires_at FROM users WHERE id = :id"),
            {"id": session["user_id"]}
        ).fetchone()
    if not row:
        return None

    token_info = {
        "access_token":  decrypt_token(row[0]),
        "refresh_token": decrypt_token(row[1]),
        "expires_at":    row[2],
        "token_type":    "Bearer",
    }

    cache = MemoryCacheHandler(token_info=token_info)
    auth = get_spotify_oauth()
    auth.cache_handler = cache

    # Refresh if expired
    if auth.is_token_expired(token_info):
        token_info = auth.refresh_access_token(token_info["refresh_token"])
        _save_token(session["user_id"], token_info)

    return spotipy.Spotify(auth=token_info["access_token"])


def _save_token(user_id, token_info):
    with get_conn() as conn:
        conn.execute(text("""
            UPDATE users SET access_token=:at, refresh_token=:rt, token_expires_at=:exp
            WHERE id=:id
        """), {
            "at":  encrypt_token(token_info["access_token"]),
            "rt":  encrypt_token(token_info.get("refresh_token", "")),
            "exp": token_info["expires_at"],
            "id":  user_id,
        })
        conn.commit()


@auth_bp.route("/login")
def login():
    state = secrets.token_hex(16)
    session['_oauth_state'] = state
    oauth = get_spotify_oauth(state=state)
    auth_url = oauth.get_authorize_url()
    return redirect(auth_url)


@auth_bp.route("/callback")
def callback():
    # Validate OAuth state to prevent CSRF on the login flow
    received_state = request.args.get("state")
    expected_state = session.pop("_oauth_state", None)
    if not received_state or received_state != expected_state:
        return redirect(url_for("main.index"))

    code = request.args.get("code")
    if not code:
        return redirect(url_for("main.index"))

    oauth = get_spotify_oauth(state=received_state)
    token_info = oauth.get_access_token(code, as_dict=True)

    sp = spotipy.Spotify(auth=token_info["access_token"])
    user = sp.current_user()

    with get_conn() as conn:
        conn.execute(text("""
            INSERT INTO users (id, display_name, avatar_url, access_token, refresh_token, token_expires_at)
            VALUES (:id, :name, :avatar, :at, :rt, :exp)
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                avatar_url=excluded.avatar_url,
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                token_expires_at=excluded.token_expires_at
        """), {
            "id":     user["id"],
            "name":   user["display_name"] or user["id"],
            "avatar": user["images"][0]["url"] if user.get("images") else None,
            "at":     encrypt_token(token_info["access_token"]),
            "rt":     encrypt_token(token_info.get("refresh_token", "")),
            "exp":    token_info["expires_at"],
        })
        conn.commit()

    session["user_id"]      = user["id"]
    session["display_name"] = user["display_name"] or user["id"]
    session["avatar_url"]   = user["images"][0]["url"] if user.get("images") else None
    return redirect(url_for("main.dashboard"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.index"))
