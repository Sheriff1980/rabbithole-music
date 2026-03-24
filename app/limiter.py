"""
Centralised Flask-Limiter instance.
Import `limiter` in blueprints and decorate routes with @limiter.limit("N per period").
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    # Sensible global defaults — specific routes add stricter limits
    default_limits=["1000 per day", "120 per minute"],
    storage_uri="memory://",
)
