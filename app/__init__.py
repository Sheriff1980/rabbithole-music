import os
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

def create_app():
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

    from .models import init_db
    init_db()

    from .routes.main import main_bp
    from .routes.generate import generate_bp
    from .routes.community import community_bp
    from .auth import auth_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(generate_bp, url_prefix="/generate")
    app.register_blueprint(community_bp, url_prefix="/community")
    app.register_blueprint(auth_bp, url_prefix="/auth")

    return app
