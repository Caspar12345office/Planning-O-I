"""
OfficeRoute -- zelfstandige applicatie.

Planningssysteem voor meubelbezorging & montage. Volledig losgekoppeld van het
Follow O-I portaal: eigen repository, eigen Render-service, eigen database en eigen
login. De volledige applicatielogica staat in planning_oi.py (Flask-blueprint).
"""

import os
import secrets
from flask import Flask, request
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
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("DATABASE_URL")),
)
app.register_blueprint(bp)   # blueprint mount op de root (url_prefix="")


# --- Performance: gzip-compressie + cache-headers (veilig, alleen stdlib) ---
import gzip as _gzip
from datetime import timedelta as _timedelta

app.config["SEND_FILE_MAX_AGE_DEFAULT"] = _timedelta(days=7)
_GZIP_TYPES = ("text/html", "text/css", "application/javascript", "application/json",
               "image/svg+xml", "text/plain", "application/manifest+json")


@app.after_request
def _perf(resp):
    # Statische bestanden lang cachen (stabiele namen + ?v=-versiestempel).
    try:
        if request.path.startswith("/static/"):
            resp.headers.setdefault("Cache-Control", "public, max-age=604800")
    except Exception:
        pass
    # Gzip tekstuele responses als de client dat ondersteunt (grote HTML/CSS/JSON).
    try:
        ae = request.headers.get("Accept-Encoding", "")
        ct = (resp.content_type or "").split(";")[0].strip()
        if ("gzip" in ae and ct in _GZIP_TYPES and resp.status_code == 200
                and "Content-Encoding" not in resp.headers
                and not resp.direct_passthrough):
            data = resp.get_data()
            if len(data) >= 800:
                gz = _gzip.compress(data, 6)
                resp.set_data(gz)
                resp.headers["Content-Encoding"] = "gzip"
                resp.headers["Content-Length"] = str(len(gz))
                resp.headers.add("Vary", "Accept-Encoding")
    except Exception:
        pass
    return resp


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5059, debug=False)
