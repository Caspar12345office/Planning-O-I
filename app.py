"""
OfficeRoute -- zelfstandige applicatie.

Planningssysteem voor meubelbezorging & montage. Volledig losgekoppeld van het
Follow O-I portaal: eigen repository, eigen Render-service, eigen database en eigen
login. De volledige applicatielogica staat in planning_oi.py (Flask-blueprint).
"""

import os
import secrets
from flask import Flask
from planning_oi import bp


def _load_secret_key():
    # Productie: zet SECRET_KEY als environment variable (verplicht op Render).
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    # Lokaal: bewaar een gegenereerde sleutel zodat sessies een herstart overleven.
    key_file = os.environ.get("PLANNING_OI_SECRET_FILE", ".secret_key")
    try:
        if os.path.exists(key_file):
            with open(key_file, "r", encoding="utf-8") as fh:
                saved = fh.read().strip()
                if saved:
                    return saved
        new_key = secrets.token_hex(32)
        with open(key_file, "w", encoding="utf-8") as fh:
            fh.write(new_key)
        return new_key
    except Exception:
        return secrets.token_hex(32)


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.register_blueprint(bp)   # blueprint mount op de root (url_prefix="")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5059, debug=False)
