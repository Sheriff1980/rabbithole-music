from flask import Blueprint, render_template, session, redirect, url_for
from ..auth import get_spotify_client

main_bp = Blueprint("main", __name__)

@main_bp.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("main.dashboard"))
    return render_template("index.html")

@main_bp.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("auth.login"))
    sp = get_spotify_client()
    if not sp:
        return redirect(url_for("auth.login"))

    # Get user's playlists
    playlists = []
    results = sp.current_user_playlists(limit=50)
    while results:
        for p in results["items"]:
            if p and p.get("name"):
                playlists.append({
                    "id": p["id"],
                    "name": p["name"],
                    "track_count": p["tracks"]["total"],
                    "image": p["images"][0]["url"] if p.get("images") else None,
                })
        results = sp.next(results) if results.get("next") else None

    return render_template("dashboard.html", playlists=playlists)
