"""
Planning O-I -- planningssysteem voor meubelbezorging & montage.

Een zelfstandige Flask-blueprint met een eigen database, eigen rollen/rechten en
eigen login. Volledig losgekoppeld van het Follow O-I portaal.

Status: productieklaar als testopstelling. Alle schermen werken met echte data en
echte interacties; de externe koppelingen (Shopify, Gmail, Google Maps, Route API,
Google OAuth/MFA, live GPS) hebben volledig ingerichte instelschermen en staan klaar
om met echte API-logica te worden "ingeplugd".
"""

from flask import (
    Blueprint, render_template, request, redirect, url_for, session,
    flash, jsonify, Response, abort, send_from_directory, got_request_exception, g,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3, os, json, secrets, csv, io, math, time, hmac, hashlib, base64, smtplib, threading

# Werkzeug kiest standaard 'scrypt' (geheugen-intensief -> traag op kleine servers).
# pbkdf2 met een bewust lage iteratie-telling is licht/snel; ruim voldoende i.c.m.
# de verplichte 2FA op deze interne tool. Houdt de login vlot op de kleine server.
_PW_METHOD = "pbkdf2:sha256:30000"


def _hash_pw(pw):
    return generate_password_hash(pw, method=_PW_METHOD)
import urllib.request, urllib.error
from email.message import EmailMessage
from datetime import datetime, timedelta, date

bp = Blueprint(
    "planning",
    __name__,
    url_prefix="",          # eigen Render-service: app draait op de root-URL
    template_folder="templates",
)

DB_PATH = os.environ.get("PLANNING_OI_DB_PATH", "planning_oi.db")
_dirn = os.path.dirname(DB_PATH)
if _dirn:
    try:
        os.makedirs(_dirn, exist_ok=True)
    except Exception:
        DB_PATH = "planning_oi.db"

# Map voor geüploade pakbonnen (PDF). Op het gratis Render-plan tijdelijk; via env naar disk.
UPLOAD_DIR = os.environ.get("PLANNING_OI_UPLOADS", os.path.join(_dirn or ".", "oi_uploads"))
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
except Exception:
    UPLOAD_DIR = "oi_uploads"
    os.makedirs(UPLOAD_DIR, exist_ok=True)

# Merknaam van de tool (overal getoond).
BRAND = "OfficeRoute"

# Vaste laad-/loslocatie: alle routes eindigen hier zodat de bus opnieuw geladen wordt.
HOME_BASE = "Breda"
BREDA = (51.5719, 4.7683)
LOAD_MARGIN_KG = 500   # het is normaal om iets over de max te zitten; pas boven deze marge een waarschuwing
# Grens waarboven een order als "belangrijke order" geldt (euro).
IMPORTANT_THRESHOLD = 3000
# File/werkzaamheden pas tonen/notificeren vanaf deze extra vertraging (minuten).
ALERT_THRESHOLD = 20

# Wie verlof-/afspraakaanvragen goedkeurt (vaste personen, per type aanvrager).
APPROVERS_MONTEUR = {"caspar@office-interior.nl", "aleks@office-interior.nl",
                     "stijn@office-interior.nl", "jorik@office-interior.nl"}
APPROVERS_OFFICE = {"caspar@office-interior.nl", "aleks@office-interior.nl", "jorik@office-interior.nl"}

# Globale coördinaten van veelgebruikte plaatsen (NL + BE) voor kaart + afstand/ETA.
CITY_COORDS = {
    "Breda": (51.5719, 4.7683), "Tilburg": (51.5606, 5.0919),
    "Eindhoven": (51.4416, 5.4697), "Utrecht": (52.0907, 5.1214),
    "Den Haag": (52.0705, 4.3007), "Rotterdam": (51.9244, 4.4777),
    "Amsterdam": (52.3676, 4.9041), "Den Bosch": (51.6978, 5.3037),
    "Leiden": (52.1601, 4.4970), "Groningen": (53.2194, 6.5665),
    "Papendrecht": (51.8302, 4.6890), "Zwolle": (52.5168, 6.0830),
    "Antwerpen": (51.2194, 4.4025), "Gent": (51.0543, 3.7174),
    "Brussel": (50.8503, 4.3517), "Hasselt": (50.9307, 5.3378),
}

# Plaats -> provincie/regio (voor het planningoverzicht).
PROVINCE = {
    "Breda": "Noord-Brabant", "Tilburg": "Noord-Brabant", "Eindhoven": "Noord-Brabant",
    "Den Bosch": "Noord-Brabant", "Rotterdam": "Zuid-Holland", "Den Haag": "Zuid-Holland",
    "Leiden": "Zuid-Holland", "Papendrecht": "Zuid-Holland", "Amsterdam": "Noord-Holland",
    "Utrecht": "Utrecht", "Groningen": "Groningen", "Zwolle": "Overijssel",
    "Antwerpen": "Antwerpen (BE)", "Gent": "Oost-Vlaanderen (BE)",
    "Brussel": "Brussel (BE)", "Hasselt": "Limburg (BE)",
}


def region_for(cities):
    """Geef een leesbare regio-aanduiding voor een set plaatsen."""
    provs = []
    for c in cities:
        p = PROVINCE.get(c)
        if p and p not in provs:
            provs.append(p)
    return " · ".join(provs) if provs else "—"


# --------------------------------------------------------------------------- #
#  Database-laag: SQLite (lokaal/standaard) of PostgreSQL (als DATABASE_URL is gezet).
#  Zo kunnen de kantoorsoftware en de monteur-app als aparte services dezelfde
#  PostgreSQL-database delen.
# --------------------------------------------------------------------------- #
import re as _re
_PG_URL = os.environ.get("DATABASE_URL", "")
if _PG_URL.startswith("postgres://"):
    _PG_URL = _PG_URL.replace("postgres://", "postgresql://", 1)
IS_PG = bool(_PG_URL)

# Connection pool voor PostgreSQL: hergebruikt verbindingen i.p.v. per request
# een nieuwe op te zetten (scheelt de TCP/TLS-handshake bij elk verzoek -> veel
# sneller bij meerdere gelijktijdige gebruikers). max_size bewust laag gehouden
# omdat de monteur-app dezelfde database deelt.
_PG_POOL = None
_PG_POOL_TRIED = False


def _get_pg_pool():
    global _PG_POOL, _PG_POOL_TRIED
    if _PG_POOL is not None or _PG_POOL_TRIED:
        return _PG_POOL
    _PG_POOL_TRIED = True
    try:
        from psycopg_pool import ConnectionPool
        _PG_POOL = ConnectionPool(_PG_URL, min_size=1, max_size=5,
                                  kwargs={"autocommit": True}, timeout=10, open=True)
    except Exception:
        _PG_POOL = None  # val terug op directe verbindingen
    return _PG_POOL


# Versiestempel (Render zet RENDER_GIT_COMMIT automatisch) — zo is via /version
# te controleren welke build live staat, en het dient als keep-alive-doel.
APP_VERSION = (os.environ.get("RENDER_GIT_COMMIT") or "dev")[:12]

# Onafgehandelde fouten in de Python-backend opvangen (voor het commandocentrum).
import collections as _collections
_APP_ERRORS = _collections.deque(maxlen=25)


def _record_app_error(sender=None, exception=None, **extra):
    try:
        _APP_ERRORS.append({"ts": time.time(),
                            "path": getattr(request, "path", "?"),
                            "err": (type(exception).__name__ + ": " + str(exception))[:160]})
    except Exception:
        pass


got_request_exception.connect(_record_app_error)


# Tabellen zonder autonummer-kolom 'id' (krijgen geen RETURNING id).
_NO_ID_TABLES = {"monteur_location", "route_closed", "integrations", "settings",
                 "order_magazijn", "voormontage_done", "route_pick", "day_roster", "monteur_day_gps"}


def _sub_placeholders(sql):
    """Vervang '?'-parameters door '%s', maar laat vraagtekens BINNEN
    string-literals ('…') met rust (anders telt psycopg te veel placeholders)."""
    out = []
    in_str = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            if in_str and i + 1 < n and sql[i + 1] == "'":  # geëscapete '' binnen tekst
                out.append("'"); i += 2; continue
            in_str = not in_str
            i += 1; continue
        out.append("%s" if (ch == "?" and not in_str) else ch)
        i += 1
    return "".join(out)


def _xlate(sql):
    """Vertaal SQLite-SQL naar PostgreSQL. Geeft (sql, append_returning) terug."""
    is_ignore = "INSERT OR IGNORE" in sql.upper()
    s = _re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', sql, flags=_re.I)
    s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    s = s.replace(" BLOB", " BYTEA")
    s = s.replace("qty || 'x ' || name", "qty::text || 'x ' || name")
    s = _re.sub(r'GROUP_CONCAT\s*\(', 'string_agg(', s, flags=_re.I)  # SQLite -> PostgreSQL
    s = _sub_placeholders(s)
    s = _re.sub(r'\bLIKE\b', 'ILIKE', s)
    if is_ignore and "ON CONFLICT" not in s.upper():
        s += " ON CONFLICT DO NOTHING"
    up = s.lstrip().upper()
    m = _re.match(r'INSERT\s+INTO\s+([a-z_]+)', s.lstrip(), flags=_re.I)
    target = (m.group(1).lower() if m else "")
    append_returning = (up.startswith("INSERT") and "RETURNING" not in up
                        and "ON CONFLICT" not in up and target not in _NO_ID_TABLES)
    if append_returning:
        s += " RETURNING id"
    return s, append_returning


class _Row:
    __slots__ = ("_c", "_v")
    def __init__(self, cols, vals):
        self._c = cols; self._v = vals
    def __getitem__(self, k):
        return self._v[k] if isinstance(k, int) else self._v[self._c.index(k)]
    def keys(self):
        return self._c
    def get(self, k, d=None):
        try:
            return self[k]
        except Exception:
            return d


def _pg_rowfactory(cur):
    cols = [d.name for d in cur.description] if cur.description else []
    def make(values):
        return _Row(cols, list(values))
    return make


class _PgCur:
    def __init__(self, conn):
        self._conn = conn
        self._cur = conn._raw.cursor(row_factory=_pg_rowfactory)
        self.lastrowid = None
        self._scalar = None
    def execute(self, sql, params=()):
        if "last_insert_rowid()" in sql.lower():
            self._scalar = self._conn._lastid
            return self
        s, ret = _xlate(sql)
        self._cur.execute(s, tuple(params) if params else None)
        if ret:
            try:
                row = self._cur.fetchone()
                self.lastrowid = row[0]
                self._conn._lastid = row[0]
            except Exception:
                pass
        return self
    def executescript(self, script):
        for stmt in script.split(";"):
            if stmt.strip():
                self.execute(stmt)
        return self
    def fetchone(self):
        if self._scalar is not None:
            v = self._scalar; self._scalar = None
            return [v]
        return self._cur.fetchone()
    def fetchall(self):
        return self._cur.fetchall()
    def __iter__(self):
        return iter(self._cur)


class _PgConn:
    def __init__(self):
        self._pool = _get_pg_pool()
        if self._pool is not None:
            try:
                self._raw = self._pool.getconn()
            except Exception:
                self._pool = None
        if self._pool is None:
            import psycopg
            self._raw = psycopg.connect(_PG_URL, autocommit=True)
        self._lastid = None
    def cursor(self):
        return _PgCur(self)
    def execute(self, sql, params=()):
        return _PgCur(self).execute(sql, params)
    def executescript(self, script):
        return _PgCur(self).executescript(script)
    def commit(self):
        pass
    def close(self):
        try:
            if self._pool is not None:
                self._pool.putconn(self._raw)
            else:
                self._raw.close()
        except Exception:
            pass


def db():
    if IS_PG:
        return _PgConn()
    # WAL + busy_timeout: meerdere gebruikers tegelijk lezen/schrijven zonder locks.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def haversine(a, b):
    """Afstand in km tussen twee (lat, lng)-punten."""
    (la1, lo1), (la2, lo2) = a, b
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dphi = math.radians(la2 - la1)
    dlmb = math.radians(lo2 - lo1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(h)))


def fmt_duration(minutes):
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    return (f"{h} u {m} min" if h else f"{m} min")


# --------------------------------------------------------------------------- #
#  Rollen & rechten
# --------------------------------------------------------------------------- #
PERMISSIONS = [
    ("view_planning",      "Planning bekijken",          "Planning"),
    ("edit_planning",      "Planning wijzigen",          "Planning"),
    ("plan_orders",        "Orders inplannen",           "Planning"),
    ("assign_monteurs",    "Monteurs toewijzen",         "Planning"),
    ("edit_routes",        "Routes wijzigen",            "Routes"),
    ("optimize_routes",    "Routes optimaliseren",       "Routes"),
    ("inform_clients",     "Klanten informeren",         "Klant"),
    ("view_orders",        "Orders bekijken",            "Orders"),
    ("edit_clients",       "Klantgegevens aanpassen",    "Klant"),
    ("view_emails",        "E-mails bekijken",           "Klant"),
    ("view_invoices",      "Factuurinformatie",          "Financieel"),
    ("complete_deliveries","Leveringen afronden",        "Orders"),
    ("view_preassembly",   "Voormontage (magazijn)",     "Magazijn"),
    ("view_magazijn",      "Magazijn live-status inzien","Magazijn"),
    ("magazijn_app",       "Magazijn-app gebruiken",     "Magazijn"),
    ("view_documents",     "Documenten",                 "Documenten"),
    ("view_connections",   "Koppelingen (commandocentrum)", "Koppelingen"),
    ("manage_freedays",    "Vrije dagen beheren",        "Personeel"),
    ("view_reports",       "Kilometerrapportage",        "Rapportage"),
    ("view_performance",   "Monteursprestaties",         "Rapportage"),
    ("view_signatures",    "Handtekeningen inzien",      "Rapportage"),
    ("view_speed",         "Snelheid monteur zien",      "Rapportage"),
    ("export",             "Exporteren",                 "Rapportage"),
    ("view_kpis",          "KPI's & omzet inzien",       "Rapportage"),
    ("view_personnel",     "Personeelsgegevens",         "Personeel"),
    ("view_hours",         "Urenregister inzien",        "Personeel"),
    ("manage_users",       "Gebruikersbeheer",           "Beheer"),
    ("manage_roles",       "Rollen & rechten beheren",   "Beheer"),
    ("manage_integrations","Koppelingen beheren",        "Beheer"),
    ("manage_settings",    "Bedrijfsinstellingen",       "Beheer"),
    ("monteur_app",        "Monteur-app gebruiken",      "Monteur"),
]
PERMISSION_KEYS = [k for k, _, _ in PERMISSIONS]
ALL_PERMS = list(PERMISSION_KEYS)

ROLE_DEFAULTS = {
    "beheerder": ALL_PERMS,
    "manager": ["view_kpis", "view_planning", "view_reports", "view_performance",
                "view_signatures", "view_speed", "view_orders", "view_invoices",
                "view_personnel", "export", "view_emails", "view_preassembly", "view_magazijn",
                "view_documents", "view_connections"],
    "planner": ["view_planning", "edit_planning", "plan_orders", "assign_monteurs",
                "edit_routes", "optimize_routes", "inform_clients", "manage_freedays",
                "view_reports", "view_orders", "view_personnel", "view_preassembly", "view_magazijn",
                "view_documents", "view_connections"],
    "administratie": ["view_orders", "edit_clients", "view_emails", "view_invoices",
                      "view_planning", "complete_deliveries", "view_documents", "view_connections"],
    "monteur": ["monteur_app"],
    "picker": ["magazijn_app"],
}
ROLE_LABELS = {"beheerder": "Beheerder", "manager": "Manager", "planner": "Planner",
               "administratie": "Administratie", "monteur": "Monteur", "picker": "Picker"}
# Vaste kantoorploeg voor de bezetting op het dashboard (los van rollen/accounts).
OFFICE_STAFF = ["Aleks", "Caspar", "Chris", "Jorik", "Stijn Pas", "Thom", "Yelith"]


# --------------------------------------------------------------------------- #
#  Koppelingen (integraties)
# --------------------------------------------------------------------------- #
INTEGRATIONS = [
    {"key": "shopify", "name": "Shopify", "icon": "🛍",
     "desc": "Realtime import van bevestigde orders als 'Nog in te plannen'. Ordernummers komen overeen met Shopify.",
     "fields": [
        {"key": "shop_url", "label": "Shop-URL", "type": "text", "placeholder": "office-interior.myshopify.com"},
        {"key": "webhook_secret", "label": "Webhook-secret (verplicht voor de import)", "type": "password"},
        {"key": "api_key", "label": "API-sleutel (optioneel — alleen voor backfill)", "type": "password", "optional": True},
        {"key": "api_secret", "label": "API-secret (optioneel — alleen voor backfill)", "type": "password", "optional": True},
        {"key": "access_token", "label": "Admin API-token (voor backfill + artikelen importeren)", "type": "password", "optional": True,
         "help": "Nodig om actieve artikelen te importeren onder Instellingen → Artikelen. Maak in Shopify een custom app met recht 'read_products' en kopieer de Admin API access token (shpat_…)."},
        {"key": "import_drafts", "label": "Draft orders importeren", "type": "toggle", "default": "0",
         "lock_off": True, "help": "Beveiligd: draft orders worden NOOIT automatisch geïmporteerd."},
        {"key": "auto_sync", "label": "Automatisch synchroniseren", "type": "toggle", "default": "1"}]},
    {"key": "gmail", "name": "Gmail (centrale mailbox)", "icon": "✉",
     "desc": "Toon volledige e-mailhistorie per klant vanuit één centrale mailbox.",
     "fields": [
        {"key": "mailbox", "label": "Centrale mailbox", "type": "text", "placeholder": "planning@office-interior.nl"},
        {"key": "client_id", "label": "OAuth client-ID", "type": "password"},
        {"key": "client_secret", "label": "OAuth client-secret", "type": "password"},
        {"key": "label_filter", "label": "Labelfilter (optioneel)", "type": "text", "placeholder": "Bezorging"}]},
    {"key": "google_maps", "name": "Google Maps", "icon": "🗺",
     "desc": "Kaarten, live locatie en navigatie in de monteur-app en op het dashboard.",
     "fields": [{"key": "api_key", "label": "Maps API-sleutel", "type": "password"}]},
    {"key": "route_api", "name": "Route Optimization", "icon": "🧭",
     "desc": "Automatische routeoptimalisatie (afstand, verkeer, capaciteit, werktijd).",
     "fields": [
        {"key": "provider", "label": "Provider", "type": "select",
         "options": ["Google Route Optimization", "OptaPlanner", "Routific", "Anders"]},
        {"key": "api_key", "label": "API-sleutel", "type": "password"},
        {"key": "max_worktime", "label": "Max. werktijd per dag (uur)", "type": "text", "placeholder": "9"},
        {"key": "depot", "label": "Eindpunt (depot)", "type": "text", "default": HOME_BASE}]},
    {"key": "gps", "name": "Live GPS-tracking", "icon": "📍",
     "desc": "Realtime locatie van monteurs op het dashboard en veilige klant-trackinglink (Uber/Picnic-stijl). De monteur-app deelt de live locatie via de telefoon.",
     "fields": [
        {"key": "provider", "label": "GPS-bron", "type": "text", "placeholder": "App-GPS (telefoon monteur) / Samsara / Webfleet"},
        {"key": "api_key", "label": "API-sleutel (optioneel)", "type": "password"},
        {"key": "share_precise", "label": "Exacte locatie delen met klant", "type": "toggle", "default": "0",
         "lock_off": True, "help": "Klant ziet altijd alleen een veilige benadering, nooit exacte GPS."}]},
    {"key": "velocity", "name": "VeloCity (busregistratie)", "icon": "🚐",
     "desc": "Koppeling met VeloCity voor voertuig- en kilometerregistratie van de bussen. Kilometers en ritten worden automatisch ingelezen per voertuig.",
     "fields": [
        {"key": "account_id", "label": "VeloCity account-ID", "type": "text"},
        {"key": "api_key", "label": "API-sleutel", "type": "password"},
        {"key": "fleet_id", "label": "Wagenpark-ID (fleet)", "type": "text", "placeholder": "bv. OI-FLEET-01"},
        {"key": "auto_import_km", "label": "Kilometers automatisch importeren", "type": "toggle", "default": "1"}]},
    {"key": "google_oauth", "name": "Google OAuth + MFA", "icon": "🔐",
     "desc": "Inloggen met Google en verplichte multi-factor authenticatie.",
     "fields": [
        {"key": "client_id", "label": "OAuth client-ID", "type": "password"},
        {"key": "client_secret", "label": "OAuth client-secret", "type": "password"},
        {"key": "require_mfa", "label": "MFA verplicht", "type": "toggle", "default": "1"},
        {"key": "allowed_domain", "label": "Toegestaan domein", "type": "text", "placeholder": "office-interior.nl"}]},
    {"key": "email", "name": "Klantmail & tracking", "icon": "📨",
     "desc": "Klantmails via de Resend-API (werkt op Render; SMTP wordt door Render geblokkeerd).",
     "fields": [
        {"key": "resend_api_key", "label": "Resend API-sleutel", "type": "password",
         "help": "Maak gratis een account op resend.com → API Keys → maak een sleutel (begint met re_)."},
        {"key": "from_email", "label": "Afzender-e-mail", "type": "text", "placeholder": "planning@office-interior.com",
         "help": "Voor je eigen domein: verifieer office-interior.com in Resend. Testen kan met onboarding@resend.dev."},
        {"key": "from_name", "label": "Afzendernaam", "type": "text", "default": "Office-Interior"},
        {"key": "reply_to", "label": "Antwoorden naar (Reply-To)", "type": "text", "placeholder": "planning@office-interior.com",
         "help": "Waar antwoorden van klanten binnenkomen. Bijv. planning@office-interior.com. Leeg = geen apart antwoordadres."},
        {"key": "smtp_host", "label": "SMTP-host (optioneel, alleen lokaal)", "type": "text", "placeholder": "leeg laten bij Resend"},
        {"key": "smtp_user", "label": "SMTP-gebruiker (optioneel)", "type": "text"},
        {"key": "smtp_pass", "label": "SMTP-wachtwoord (optioneel)", "type": "password"},
        {"key": "send_live", "label": "E-mails écht versturen", "type": "toggle", "default": "0",
         "help": "UIT = testmodus: er wordt NIETS echt verstuurd (alleen opgeslagen/gelogd; 2FA-code op het scherm). Zet pas AAN als je live wilt."},
        {"key": "send_delay_updates", "label": "Automatische vertraging-updates", "type": "toggle", "default": "1"}]},
    {"key": "backup", "name": "Back-ups", "icon": "💾",
     "desc": "Automatische dagelijkse back-up van de volledige database.",
     "fields": [
        {"key": "enabled", "label": "Automatische back-up", "type": "toggle", "default": "1"},
        {"key": "frequency", "label": "Frequentie", "type": "select", "options": ["Dagelijks", "Elke 6 uur", "Wekelijks"]},
        {"key": "destination", "label": "Bestemming", "type": "text", "placeholder": "gs://oi-backups of Azure Blob"}]},
]
INTEGRATION_BY_KEY = {i["key"]: i for i in INTEGRATIONS}


# --------------------------------------------------------------------------- #
#  Schema + seed
# --------------------------------------------------------------------------- #
def _new_track_token():
    # Niet-raadbare, URL-veilige tracking-token (voorkomt enumeratie van ordernummers).
    return secrets.token_urlsafe(16)


def init_db():
    conn = db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
        role TEXT NOT NULL, permissions TEXT, phone TEXT,
        monteur_id INTEGER, active INTEGER NOT NULL DEFAULT 1, created_at TEXT, last_seen TEXT);
    CREATE TABLE IF NOT EXISTS chat_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, text TEXT, order_number TEXT, ts TEXT);
    CREATE TABLE IF NOT EXISTS deliveries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER, monteur_id INTEGER, receiver TEXT, signature TEXT,
        outcome TEXT, sub_outcome TEXT, ts TEXT);
    CREATE TABLE IF NOT EXISTS route_closed(
        monteur_id INTEGER, date TEXT, ts TEXT, PRIMARY KEY(monteur_id, date));
    CREATE TABLE IF NOT EXISTS leave_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, user_name TEXT, is_monteur INTEGER, monteur_id INTEGER,
        category TEXT, leave_type TEXT, date_from TEXT, date_to TEXT,
        time_from TEXT, time_to TEXT, reason TEXT,
        status TEXT DEFAULT 'open', decided_by TEXT, decision_reason TEXT,
        decided_at TEXT, decided_seen INTEGER DEFAULT 0, created_at TEXT);
    CREATE TABLE IF NOT EXISTS clients(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT, phone TEXT,
        address TEXT, postal TEXT, city TEXT, invoice_address TEXT,
        notes TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_number TEXT, client_id INTEGER, source TEXT DEFAULT 'manual',
        is_draft INTEGER DEFAULT 0, status TEXT DEFAULT 'in_te_plannen',
        delivery_address TEXT, city TEXT, postal TEXT,
        invoice_address TEXT, phone TEXT, email TEXT,
        desired_date TEXT, notes TEXT, instructions TEXT, customer_note TEXT,
        amount REAL DEFAULT 0, volume REAL DEFAULT 0, weight REAL DEFAULT 0,
        montage_min INTEGER DEFAULT 30, service_type TEXT DEFAULT 'montage',
        pakbon TEXT, shopify_id TEXT, track_token TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS order_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, name TEXT, qty INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS monteurs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, phone TEXT, email TEXT,
        speed INTEGER DEFAULT 3, color TEXT, bus_id INTEGER,
        home_address TEXT, home_lat REAL, home_lng REAL,
        standard INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS busses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, plate TEXT, driver TEXT,
        max_volume REAL DEFAULT 12, max_weight REAL DEFAULT 1200,
        max_stops INTEGER DEFAULT 12, apk_date TEXT, maintenance TEXT,
        active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS planning(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER UNIQUE, monteur_id INTEGER, bus_id INTEGER,
        date TEXT, slot_start TEXT, slot_end TEXT, sequence INTEGER DEFAULT 0,
        confirmed INTEGER DEFAULT 0, mailed INTEGER DEFAULT 0,
        arrival_mailed INTEGER DEFAULT 0, delay_mailed INTEGER DEFAULT 0, status TEXT DEFAULT 'gepland');
    CREATE TABLE IF NOT EXISTS free_days(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        monteur_id INTEGER, type TEXT, date_from TEXT, date_to TEXT, note TEXT);
    CREATE TABLE IF NOT EXISTS integrations(
        ikey TEXT, field TEXT, value TEXT, PRIMARY KEY(ikey, field));
    CREATE TABLE IF NOT EXISTS email_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER, direction TEXT, subject TEXT, body TEXT, ts TEXT, has_attachment INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS settings(skey TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS bus_issues(id INTEGER PRIMARY KEY AUTOINCREMENT, monteur_id INTEGER, monteur_name TEXT,
        reporter_email TEXT, bus_label TEXT, plate TEXT, message TEXT, status TEXT DEFAULT 'open',
        created_at TEXT, resolved_by TEXT, resolved_at TEXT);
    CREATE TABLE IF NOT EXISTS monteur_location(
        monteur_id INTEGER PRIMARY KEY, lat REAL, lng REAL, updated_at TEXT, live INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS office_days(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        person TEXT, date TEXT, status TEXT, note TEXT, UNIQUE(person, date));
    CREATE TABLE IF NOT EXISTS vehicle_km(
        id INTEGER PRIMARY KEY AUTOINCREMENT, bus_id INTEGER, date TEXT, km REAL, UNIQUE(bus_id, date));
    CREATE TABLE IF NOT EXISTS team_questions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user_id INTEGER, to_user_id INTEGER, order_id INTEGER,
        text TEXT, ts TEXT, resolved INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS documents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, description TEXT, filename TEXT, mimetype TEXT, size INTEGER,
        data BLOB, uploaded_by TEXT, uploaded_at TEXT);
    CREATE TABLE IF NOT EXISTS notepad(
        id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, updated_by TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS notepad_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, person TEXT, ts TEXT, content TEXT);
    CREATE TABLE IF NOT EXISTS work_hours(
        id INTEGER PRIMARY KEY AUTOINCREMENT, monteur_id INTEGER, user_id INTEGER,
        user_email TEXT, user_name TEXT, work_date TEXT, start_time TEXT, end_time TEXT,
        worked_min INTEGER DEFAULT 0, overtime_min INTEGER DEFAULT 0, note TEXT, submitted_at TEXT,
        UNIQUE(monteur_id, work_date));
    CREATE TABLE IF NOT EXISTS monteur_day_gps(
        monteur_id INTEGER, date TEXT, home_since TEXT, PRIMARY KEY(monteur_id, date));
    CREATE TABLE IF NOT EXISTS bus_notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT, bus_id INTEGER, note TEXT, important INTEGER DEFAULT 0,
        image_data BLOB, image_mime TEXT, image_name TEXT, author TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS order_magazijn(
        order_id INTEGER PRIMARY KEY, gepickt_door TEXT, gecontroleerd_door TEXT,
        klaargezet INTEGER DEFAULT 0, klaargezet_at TEXT, picker_note TEXT, updated_at TEXT,
        manco INTEGER DEFAULT 0, manco_note TEXT, manco_by TEXT, manco_at TEXT,
        manco_resolved_by TEXT, manco_resolved_at TEXT);
    CREATE TABLE IF NOT EXISTS voormontage_done(
        work_date TEXT, item_name TEXT, done INTEGER DEFAULT 0, done_by TEXT, done_at TEXT,
        PRIMARY KEY(work_date, item_name));
    CREATE TABLE IF NOT EXISTS picker_tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, picker_id INTEGER, picker_name TEXT, text TEXT,
        assigned_by TEXT, created_at TEXT, done INTEGER DEFAULT 0, done_at TEXT);
    CREATE TABLE IF NOT EXISTS office_notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT, text TEXT, created_at TEXT, seen INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS route_pick(
        monteur_id INTEGER, date TEXT, status TEXT DEFAULT 'bezig', picker_name TEXT,
        started_at TEXT, updated_at TEXT, PRIMARY KEY(monteur_id, date));
    CREATE TABLE IF NOT EXISTS day_roster(
        date TEXT, monteur_id INTEGER, PRIMARY KEY(date, monteur_id));
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, display_name TEXT,
        m1 INTEGER DEFAULT 0, m2 INTEGER DEFAULT 0, m3 INTEGER DEFAULT 0,
        m4 INTEGER DEFAULT 0, m5 INTEGER DEFAULT 0,
        l1 INTEGER DEFAULT 0, l2 INTEGER DEFAULT 0, l3 INTEGER DEFAULT 0,
        l4 INTEGER DEFAULT 0, l5 INTEGER DEFAULT 0, active INTEGER DEFAULT 1, created_at TEXT);
    """)
    # Defensieve migratie (bv. bestaande database met disk op Render).
    for stmt in ("ALTER TABLE users ADD COLUMN last_seen TEXT",
                 "ALTER TABLE planning ADD COLUMN mailed INTEGER DEFAULT 0",
                 "ALTER TABLE orders ADD COLUMN service_type TEXT DEFAULT 'montage'",
                 "ALTER TABLE orders ADD COLUMN pakbon TEXT",
                 "ALTER TABLE orders ADD COLUMN fulfilled INTEGER DEFAULT 0",
                 "ALTER TABLE orders ADD COLUMN fulfilled_at TEXT",
                 "ALTER TABLE planning ADD COLUMN arrival_mailed INTEGER DEFAULT 0",
                 "ALTER TABLE planning ADD COLUMN delay_mailed INTEGER DEFAULT 0",
                 "ALTER TABLE monteurs ADD COLUMN standard INTEGER NOT NULL DEFAULT 1",
                 "ALTER TABLE orders ADD COLUMN customer_note TEXT",
                 "ALTER TABLE order_items ADD COLUMN picked INTEGER DEFAULT 0",
                 "ALTER TABLE order_magazijn ADD COLUMN manco INTEGER DEFAULT 0",
                 "ALTER TABLE order_magazijn ADD COLUMN manco_note TEXT",
                 "ALTER TABLE order_magazijn ADD COLUMN manco_by TEXT",
                 "ALTER TABLE order_magazijn ADD COLUMN manco_at TEXT",
                 "ALTER TABLE order_magazijn ADD COLUMN manco_resolved_by TEXT",
                 "ALTER TABLE order_magazijn ADD COLUMN manco_resolved_at TEXT",
                 "ALTER TABLE orders ADD COLUMN track_token TEXT",
                 "ALTER TABLE products ADD COLUMN l1 INTEGER DEFAULT 0",
                 "ALTER TABLE products ADD COLUMN l2 INTEGER DEFAULT 0",
                 "ALTER TABLE products ADD COLUMN l3 INTEGER DEFAULT 0",
                 "ALTER TABLE products ADD COLUMN l4 INTEGER DEFAULT 0",
                 "ALTER TABLE products ADD COLUMN l5 INTEGER DEFAULT 0",
                 "ALTER TABLE order_items ADD COLUMN montage_custom INTEGER",
                 "ALTER TABLE busses ADD COLUMN empty_weight REAL DEFAULT 0",
                 "ALTER TABLE products ADD COLUMN weight_kg REAL DEFAULT 0"):
        try:
            conn.execute(stmt)
        except Exception:
            pass
    # Backfill: geef bestaande orders zonder tracking-token er alsnog een (idempotent).
    try:
        for r in conn.execute("SELECT id FROM orders WHERE track_token IS NULL OR track_token=''").fetchall():
            conn.execute("UPDATE orders SET track_token=? WHERE id=?", (_new_track_token(), r["id"]))
        conn.commit()
    except Exception:
        pass
    # Eenmalige opschoning bestaande orderregels: merk 'Renab' weghalen (idempotent).
    try:
        conn.execute("UPDATE order_items SET name=TRIM(SUBSTR(name,7)) WHERE name LIKE 'Renab %'")
        conn.commit()
    except Exception:
        pass
    # Indexen op veelgebruikte kolommen — houdt queries snel bij meerdere
    # gelijktijdige gebruikers (planning per dag/monteur, orderregels, statussen).
    for idx in ("CREATE INDEX IF NOT EXISTS idx_planning_date ON planning(date)",
                "CREATE INDEX IF NOT EXISTS idx_planning_order ON planning(order_id)",
                "CREATE INDEX IF NOT EXISTS idx_planning_monteur_date ON planning(monteur_id, date)",
                "CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id)",
                "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
                "CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_id)",
                "CREATE INDEX IF NOT EXISTS idx_orders_desired ON orders(desired_date)",
                "CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_messages(ts)",
                "CREATE INDEX IF NOT EXISTS idx_email_log_client ON email_log(client_id)",
                "CREATE INDEX IF NOT EXISTS idx_team_q_to ON team_questions(to_user_id, resolved)",
                "CREATE INDEX IF NOT EXISTS idx_deliveries_monteur_ts ON deliveries(monteur_id, ts)",
                "CREATE INDEX IF NOT EXISTS idx_orders_track_token ON orders(track_token)",
                "CREATE INDEX IF NOT EXISTS idx_planning_status ON planning(status)"):
        try:
            conn.execute(idx)
        except Exception:
            pass
    conn.commit()
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        _seed(conn)
    conn.close()


def _seed(conn):
    c = conn.cursor()
    today = datetime.now().date()

    def iso(d):
        return d.isoformat()

    # bussen
    for b in [
        ("Bus 1 - Mercedes Sprinter", "VND-12-A", "Rick", 14, 1400, 14, iso(today + timedelta(days=120)), ""),
        ("Bus 2 - VW Crafter", "8-XGT-99", "Sven", 12, 1200, 12, iso(today + timedelta(days=40)), "Kleine servicebeurt gepland"),
        ("Bus 3 - Ford Transit", "GV-880-K", "Youssef", 10, 1000, 10, iso(today + timedelta(days=8)), "APK loopt bijna af"),
    ]:
        c.execute("""INSERT INTO busses(name,plate,driver,max_volume,max_weight,max_stops,apk_date,maintenance)
                     VALUES(?,?,?,?,?,?,?,?)""", b)

    # monteurs: speed 1-5 + thuisadres (vertrekpunt route); standard=0 = incidenteel (niet altijd zichtbaar)
    # (name, phone, email, speed, color, bus_id, home_address, lat, lng, standard)
    monteurs = [
        ("Tom", "06-21110011", "tom@office-interior.nl", 5, "#0f3d3e", 1, "Ginnekenweg 200, Breda", 51.5700, 4.7800, 1),
        ("Stijn v Gurp", "06-21110022", "stijnvgurp@office-interior.nl", 4, "#b88a44", 2, "Ringbaan Oost 100, Tilburg", 51.5550, 5.1050, 1),
        ("Stijn Pas", "06-21110033", "stijnpas@office-interior.nl", 4, "#15595a", 3, "Aalsterweg 50, Eindhoven", 51.4200, 5.4800, 0),
        ("Jorik", "06-21110044", "jorik@office-interior.nl", 3, "#6d5bd0", None, "Pettelaarpark 5, Den Bosch", 51.6900, 5.3000, 0),
        ("Delon", "06-21110055", "delon@office-interior.nl", 4, "#2f6df0", None, "Coolsingel 5, Rotterdam", 51.9200, 4.4800, 1),
        ("Sem", "06-21110066", "sem@office-interior.nl", 3, "#c2410c", None, "Biltstraat 10, Utrecht", 52.0930, 5.1250, 1),
        ("Mathijs", "06-21110077", "mathijs@office-interior.nl", 5, "#3b6d11", None, "Haagweg 10, Breda", 51.5800, 4.7600, 1),
    ]
    for m in monteurs:
        c.execute("""INSERT INTO monteurs(name,phone,email,speed,color,bus_id,home_address,home_lat,home_lng,standard)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""", m)

    def mk(name, email, role, monteur_id=None, phone="", extra=None):
        perms = list(ROLE_DEFAULTS[role]) + (extra or [])
        c.execute("""INSERT INTO users(name,email,password,role,permissions,phone,monteur_id,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (name, email, _hash_pw("PlanningOI2025!"), role,
                   json.dumps(perms), phone, monteur_id, iso(today)))
    # kantoor
    mk("Caspar", "caspar@office-interior.nl", "beheerder", phone="085-0481444")
    mk("Aleks", "aleks@office-interior.nl", "beheerder")
    mk("Jorik", "jorik@office-interior.nl", "planner", monteur_id=4, extra=["monteur_app"])   # soms op de weg
    mk("Stijn", "stijn@office-interior.nl", "planner", monteur_id=3, extra=["monteur_app"])    # NIET Stijn v Gurp; soms op de weg
    mk("Chris", "chris@office-interior.nl", "planner")
    mk("Thom", "thom@office-interior.nl", "planner")
    mk("Yelith", "yelith@office-interior.nl", "planner")
    # monteur-app accounts
    mk("Tom", "tom@office-interior.nl", "monteur", monteur_id=1, phone="06-21110011")
    mk("Stijn v Gurp", "stijnvgurp@office-interior.nl", "monteur", monteur_id=2)
    mk("Delon", "delon@office-interior.nl", "monteur", monteur_id=5)
    mk("Sem", "sem@office-interior.nl", "monteur", monteur_id=6)
    mk("Mathijs", "mathijs@office-interior.nl", "monteur", monteur_id=7)

    clients = [
        ("Gemeente Tilburg", "inkoop@tilburg.nl", "013-5420000", "Stadhuisplein 130", "5038 TC", "Tilburg"),
        ("Brabant Advocaten", "office@brabantadvocaten.nl", "076-5300000", "Claudius Prinsenlaan 12", "4811 DJ", "Breda"),
        ("De Nieuwe Werkplek BV", "facilitair@dnw.nl", "040-2900000", "Kennedyplein 200", "5611 ZT", "Eindhoven"),
        ("Zorggroep West", "inkoop@zorggroepwest.nl", "010-4100000", "Coolsingel 40", "3011 AD", "Rotterdam"),
        ("Studio Noord", "hallo@studionoord.nl", "020-7700000", "Overhoeksplein 1", "1031 KS", "Amsterdam"),
        ("Tech Campus Den Bosch", "fm@techcampus.nl", "073-6100000", "Pettelaarpark 70", "5216 PP", "Den Bosch"),
        ("OfficeHub Antwerpen", "info@officehub.be", "+32 3 2000000", "Meir 1", "2000", "Antwerpen"),
        ("Kantoor Gent NV", "aankoop@kantoorgent.be", "+32 9 2100000", "Korenmarkt 5", "9000", "Gent"),
    ]
    for cl in clients:
        c.execute("""INSERT INTO clients(name,email,phone,address,postal,city,invoice_address,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""", (cl[0], cl[1], cl[2], cl[3], cl[4], cl[5], cl[3], iso(today)))

    # orders: ordernummers = Shopify-bestelnummers; amount in euro (>3000 = belangrijke order)
    # (num, client_id, source, draft, status, addr, city, postal, phone, email, days, amount, vol, weight, montage, items)
    orders = [
        ("36399", 1, "shopify", 0, "in_te_plannen", "Stadhuisplein 130", "Tilburg", "5038 TC", "013-5420000", "inkoop@tilburg.nl", 1, 5400, 3.2, 280, 60, [("Bureaustoel Pro", 8), ("Vergadertafel 240cm", 1)]),
        ("36415", 2, "shopify", 0, "in_te_plannen", "Claudius Prinsenlaan 12", "Breda", "4811 DJ", "076-5300000", "office@brabantadvocaten.nl", 2, 1290, 1.4, 120, 30, [("Boekenkast eiken", 3)]),
        ("36403", 3, "manual", 0, "in_te_plannen", "Kennedyplein 200", "Eindhoven", "5611 ZT", "040-2900000", "facilitair@dnw.nl", 2, 8600, 5.6, 540, 120, [("Zit-sta bureau", 12), ("Monitorarm", 12)]),
        ("36686", 4, "shopify", 0, "in_te_plannen", "Coolsingel 40", "Rotterdam", "3011 AD", "010-4100000", "inkoop@zorggroepwest.nl", 3, 2100, 2.1, 190, 45, [("Loungebank 3-zits", 2)]),
        ("36537", 5, "manual", 0, "in_te_plannen", "Overhoeksplein 1", "Amsterdam", "1031 KS", "020-7700000", "hallo@studionoord.nl", 4, 540, 0.9, 60, 20, [("Akoestisch paneel", 6)]),
        ("36572", 6, "shopify", 1, "draft", "Pettelaarpark 70", "Den Bosch", "5216 PP", "073-6100000", "fm@techcampus.nl", 5, 3200, 4.0, 300, 90, [("Phonebooth", 2)]),
        # toekomstige grote (belangrijke) orders
        ("36701", 6, "shopify", 0, "in_te_plannen", "Pettelaarpark 70", "Den Bosch", "5216 PP", "073-6100000", "fm@techcampus.nl", 9, 12500, 8.5, 900, 240, [("Werkplek compleet", 18), ("Akoestische wand", 4)]),
        ("36702", 4, "manual", 0, "in_te_plannen", "Coolsingel 40", "Rotterdam", "3011 AD", "010-4100000", "inkoop@zorggroepwest.nl", 12, 4300, 3.0, 260, 90, [("Directiebureau", 2), ("Kast hoog", 4)]),
        # België (we zijn ook in BE actief)
        ("36720", 7, "shopify", 0, "in_te_plannen", "Meir 1", "Antwerpen", "2000", "+32 3 2000000", "info@officehub.be", 3, 2600, 2.2, 200, 60, [("Bureau zwart", 6), ("Bureaustoel", 6)]),
        ("36721", 8, "manual", 0, "in_te_plannen", "Korenmarkt 5", "Gent", "9000", "+32 9 2100000", "aankoop@kantoorgent.be", 6, 5200, 3.4, 300, 110, [("Vergadertafel", 1), ("Kast laag", 5)]),
        # reeds gepland (vandaag)
        ("36338", 2, "shopify", 0, "gepland", "Claudius Prinsenlaan 12", "Breda", "4811 DJ", "076-5300000", "office@brabantadvocaten.nl", 0, 1850, 1.8, 150, 40, [("Bureau wit", 4)]),
        ("36339", 1, "manual", 0, "gepland", "Stadhuisplein 130", "Tilburg", "5038 TC", "013-5420000", "inkoop@tilburg.nl", 0, 2400, 2.4, 210, 50, [("Kastenwand", 1)]),
        ("36340", 4, "shopify", 0, "onderweg", "Coolsingel 40", "Rotterdam", "3011 AD", "010-4100000", "inkoop@zorggroepwest.nl", 0, 990, 1.2, 90, 25, [("Balie-element", 1)]),
    ]
    order_ids = {}
    for o in orders:
        (num, cid, source, draft, status, addr, city, postal, phone, email,
         dft, amount, vol, weight, montage, items) = o
        full_addr = f"{addr}, {postal} {city}"
        c.execute("""INSERT INTO orders(order_number,client_id,source,is_draft,status,delivery_address,city,postal,
                     invoice_address,phone,email,desired_date,amount,volume,weight,montage_min,shopify_id,track_token,created_at,notes)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (num, cid, source, draft, status, full_addr, city, postal, full_addr, phone, email,
                   iso(today + timedelta(days=dft)), amount, vol, weight, montage,
                   (f"gid://shopify/Order/{1000+int(num)}" if source == "shopify" else None), _new_track_token(), iso(today), ""))
        oid = c.lastrowid
        order_ids[num] = oid
        for nm, q in items:
            c.execute("INSERT INTO order_items(order_id,name,qty) VALUES(?,?,?)", (oid, nm, q))

    # een paar orders als 'levering' (rest blijft montage) -> Mon/Lev-kolom
    for onum in ("36339", "36686", "36537", "36340"):
        c.execute("UPDATE orders SET service_type='levering' WHERE order_number=?", (onum,))

    for num, mid, bid, seq, s, e, st, conf in [
        ("36338", 1, 1, 0, "08:30", "09:10", "afgerond", 1),
        ("36339", 1, 1, 1, "09:40", "10:30", "gepland", 0),
        ("36340", 2, 2, 0, "08:15", "08:40", "onderweg", 1),
    ]:
        c.execute("""INSERT INTO planning(order_id,monteur_id,bus_id,date,slot_start,slot_end,sequence,confirmed,status)
                     VALUES(?,?,?,?,?,?,?,?,?)""", (order_ids[num], mid, bid, iso(today), s, e, seq, conf, st))

    c.execute("INSERT INTO free_days(monteur_id,type,date_from,date_to,note) VALUES(?,?,?,?,?)",
              (4, "vakantie", iso(today + timedelta(days=2)), iso(today + timedelta(days=9)), "Zomervakantie"))
    c.execute("INSERT INTO free_days(monteur_id,type,date_from,date_to,note) VALUES(?,?,?,?,?)",
              (3, "atv", iso(today + timedelta(days=4)), iso(today + timedelta(days=4)), ""))

    c.execute("INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment) VALUES(?,?,?,?,?,?)",
              (2, "in", "Vraag over levertijd #36338", "Kunnen jullie 's ochtends leveren?", iso(today), 0))
    c.execute("INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment) VALUES(?,?,?,?,?,?)",
              (2, "out", "Re: Vraag over levertijd #36338", "Zeker, we leveren tussen 08:30 en 09:10.", iso(today), 1))

    # live GPS-posities (monteur deelt live vanaf zijn telefoon; hier geseed voor de demo)
    for mid, lat, lng, live in [(1, 51.6200, 4.9500, 1), (2, 51.5200, 5.1000, 1), (3, 52.0000, 5.1200, 0)]:
        c.execute("INSERT INTO monteur_location(monteur_id,lat,lng,updated_at,live) VALUES(?,?,?,?,?)",
                  (mid, lat, lng, datetime.now().isoformat(timespec="minutes"), live))

    # kantoorbezetting vandaag (kantoorpersoneel = niet-monteurs)
    for person, status, note in [
        ("Caspar", "kantoor", ""), ("Aleks", "kantoor", ""),
        ("Jorik", "afspraak", "Soms op de weg — incidenteel inzetbaar als monteur"),
        ("Stijn", "afspraak", "Klantbezoek Eindhoven (hele ochtend)"),
        ("Chris", "kantoor", ""), ("Thom", "kantoor", ""), ("Yelith", "kantoor", ""),
    ]:
        c.execute("INSERT INTO office_days(person,date,status,note) VALUES(?,?,?,?)",
                  (person, iso(today), status, note))

    # kilometerregistratie per voertuig (laatste 35 dagen)
    base = {1: 165, 2: 140, 3: 120}
    for n in range(35):
        d = today - timedelta(days=n)
        if d.weekday() >= 5:   # weekend overslaan
            continue
        for bid, bkm in base.items():
            km = bkm + ((n * 7 + bid * 13) % 60) - 20   # deterministische variatie
            c.execute("INSERT OR IGNORE INTO vehicle_km(bus_id,date,km) VALUES(?,?,?)", (bid, iso(d), max(40, km)))

    # voorbeeld @mention-vraag (planner -> beheer)
    c.execute("""INSERT INTO team_questions(from_user_id,to_user_id,order_id,text,ts,resolved)
                 VALUES((SELECT id FROM users WHERE email='chris@office-interior.nl'),
                        (SELECT id FROM users WHERE email='caspar@office-interior.nl'),
                        (SELECT id FROM orders WHERE order_number='36403'),
                        'Kan deze grote order van Eindhoven met 2 monteurs? Lijkt me veel montage.',
                        ?, 0)""", (datetime.now().isoformat(timespec="minutes"),))

    # teamchat (voorbeeldgesprek over lopende orders)
    now_min = datetime.now().isoformat(timespec="minutes")
    chat = [
        ("chris@office-interior.nl", "Order #36403 (Eindhoven) is groot — zal ik er 2 monteurs op zetten?", "36403"),
        ("caspar@office-interior.nl", "Ja prima, plan Tom en Stijn v Gurp samen. Ik stem de levertijd af met de klant.", "36403"),
        ("thom@office-interior.nl", "Antwerpen #36720 staat klaar, factuuradres klopt.", "36720"),
    ]
    for email, text, onum in chat:
        c.execute("""INSERT INTO chat_messages(user_id,text,order_number,ts)
                     VALUES((SELECT id FROM users WHERE email=?),?,?,?)""", (email, text, onum, now_min))

    # markeer een paar gebruikers als recent actief (live gebruikers-demo)
    for email in ("caspar@office-interior.nl", "chris@office-interior.nl"):
        c.execute("UPDATE users SET last_seen=? WHERE email=?", (datetime.now().isoformat(timespec="seconds"), email))

    for integ in INTEGRATIONS:
        for f in integ["fields"]:
            if "default" in f:
                c.execute("INSERT OR IGNORE INTO integrations(ikey,field,value) VALUES(?,?,?)",
                          (integ["key"], f["key"], f["default"]))
    c.execute("INSERT OR IGNORE INTO integrations(ikey,field,value) VALUES(?,?,?)", ("route_api", "depot", HOME_BASE))

    settings = {
        "company_name": "Office-Interior Bezorging & Montage",
        "home_base": HOME_BASE,
        "tpl_confirm": (
            "Beste {klant},\n\n"
            "Hartelijk dank voor uw bestelling bij Office-Interior. Wat fijn dat we uw nieuwe "
            "meubilair mogen komen bezorgen én voor u mogen monteren.\n\n"
            "Uw levering staat gepland op {datum}, tussen {tijdvak}. Onze monteurs plaatsen alles "
            "netjes op de gewenste plek en nemen het verpakkingsmateriaal weer mee.\n\n"
            "Komt de afspraak onverhoopt niet uit? Laat het ons gerust tijdig weten via {telefoon} "
            "of {email}, dan kijken we samen naar een nieuw moment.\n\n"
            "We kijken ernaar uit u van dienst te zijn.\n\n"
            "Met vriendelijke groet,\nTeam Office-Interior\n{telefoon} · {email}"),
        "tpl_arrival": (
            "Beste {klant},\n\n"
            "Vandaag is het zover: uw bestelling wordt geleverd.\n\n"
            "Onze monteur is onderweg en verwacht rond {eta} bij u te arriveren. Via onderstaande "
            "link volgt u live wanneer we ongeveer aankomen:\n\n{trackinglink}\n\n"
            "Zou u ervoor willen zorgen dat de ruimte goed toegankelijk is? Dan kunnen wij vlot en "
            "zorgvuldig aan de slag.\n\nTot straks!\n\n"
            "Met vriendelijke groet,\nTeam Office-Interior\n{telefoon}"),
        "tpl_delay": (
            "Beste {klant},\n\n"
            "We houden u graag eerlijk op de hoogte: door omstandigheden onderweg loopt onze "
            "aankomst iets uit. De nieuwe verwachte aankomsttijd is {eta}.\n\n"
            "Onze excuses voor het ongemak. We doen ons uiterste best om alsnog zo snel mogelijk "
            "bij u te zijn. Via {trackinglink} blijft u realtime op de hoogte.\n\n"
            "Dank voor uw begrip.\n\nMet vriendelijke groet,\nTeam Office-Interior"),
    }
    for k, v in settings.items():
        c.execute("INSERT OR IGNORE INTO settings(skey,value) VALUES(?,?)", (k, v))
    conn.commit()


# --------------------------------------------------------------------------- #
#  Auth & helpers
# --------------------------------------------------------------------------- #
def current_user():
    # Cache per request (Flask g): _inject, login_required en has_perm vragen anders
    # meermaals dezelfde user op bij elke pagina/API-call.
    if getattr(g, "_cur_user_set", False):
        return g._cur_user
    uid = session.get("p_user_id")
    u = None
    if uid:
        conn = db()
        u = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        conn.close()
    g._cur_user = u
    g._cur_user_set = True
    return u


def user_perms(u):
    if not u:
        return set()
    if u["role"] == "beheerder":
        return set(ALL_PERMS)
    try:
        return set(json.loads(u["permissions"] or "[]"))
    except Exception:
        return set()


def has_perm(perm):
    return perm in user_perms(current_user())


def online_users(within_minutes=2):
    cutoff = (datetime.now() - timedelta(minutes=within_minutes)).isoformat(timespec="seconds")
    conn = db()
    rows = conn.execute("""SELECT name, role FROM users WHERE active=1 AND last_seen IS NOT NULL
                           AND last_seen >= ? ORDER BY name""", (cutoff,)).fetchall()
    conn.close()
    return [{"name": r["name"], "role": r["role"], "initial": (r["name"][:1] or "?").upper()} for r in rows]


@bp.before_app_request
def _stamp_last_seen():
    if request.blueprint != "planning":
        return
    uid = session.get("p_user_id")
    if not uid:
        return
    # Throttle: hooguit 1 schrijf per 30s per gebruiker (minder DB-belasting bij veel users).
    now = time.time()
    if session.get("_seen_ts", 0) + 30 > now:
        return
    session["_seen_ts"] = now
    try:
        conn = db()
        conn.execute("UPDATE users SET last_seen=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), uid))
        conn.commit()
        conn.close()
    except Exception:
        pass


def setting(key, default=""):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE skey=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def integ_status(ikey):
    integ = INTEGRATION_BY_KEY[ikey]
    conn = db()
    rows = {r["field"]: r["value"] for r in
            conn.execute("SELECT field,value FROM integrations WHERE ikey=?", (ikey,)).fetchall()}
    conn.close()
    required = [f["key"] for f in integ["fields"]
                if f["type"] in ("text", "password") and not f.get("optional")]
    filled = [k for k in required if (rows.get(k) or "").strip()]
    if required and len(filled) == len(required):
        return "verbonden"
    return "deels" if filled else "niet_gekoppeld"


def open_questions_count(u):
    if not u:
        return 0
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM team_questions WHERE to_user_id=? AND resolved=0", (u["id"],)).fetchone()[0]
    conn.close()
    return n


def _email(u):
    return (u["email"] or "").lower() if u else ""


def can_approve(u, is_monteur):
    return bool(u) and _email(u) in (APPROVERS_MONTEUR if is_monteur else APPROVERS_OFFICE)


def pending_leave_count(u):
    if not u:
        return 0
    conn = db()
    n = 0
    if _email(u) in APPROVERS_MONTEUR:
        n += conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status='open' AND is_monteur=1").fetchone()[0]
    if _email(u) in APPROVERS_OFFICE:
        n += conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status='open' AND is_monteur=0").fetchone()[0]
    conn.close()
    return n


def my_unseen_decision(u):
    if not u:
        return None
    conn = db()
    r = conn.execute("""SELECT * FROM leave_requests WHERE user_id=? AND status!='open' AND decided_seen=0
                        ORDER BY decided_at DESC LIMIT 1""", (u["id"],)).fetchone()
    conn.close()
    return dict(r) if r else None


def open_bus_issues_count(u):
    if not u:
        return 0
    conn = db()
    n = conn.execute("SELECT COUNT(*) FROM bus_issues WHERE status='open'").fetchone()[0]
    conn.close()
    return n


@bp.app_context_processor
def _inject():
    if request.blueprint != "planning":
        return {}
    u = current_user()
    online = online_users() if u else []
    return {"p_user": u, "p_perms": user_perms(u), "p_has_perm": has_perm,
            "ROLE_LABELS": ROLE_LABELS, "HOME_BASE": HOME_BASE, "p_nav": NAV, "BRAND": BRAND,
            "p_open_questions": open_questions_count(u), "p_online": online,
            "p_pending_leave": pending_leave_count(u) if u else 0,
            "p_open_bus_issues": open_bus_issues_count(u) if u else 0,
            "p_leave_decision": my_unseen_decision(u) if u else None}


def login_required(perm=None):
    u = current_user()
    if not u:
        return redirect(url_for("planning.login", next=request.path))
    if perm and perm not in user_perms(u):
        return render_template("planning/no_access.html", perm=perm), 403
    return None


# Navigatie: items met 'endpoint' (link) of 'children' (uitklapbare groep onder Instellingen/Orders).
NAV = [
    {"label": "Dashboard", "endpoint": "planning.dashboard", "icon": "grid", "perm": "view_planning"},
    {"label": "Planning & routes", "endpoint": "planning.planning", "icon": "calendar", "perm": "view_planning"},
    {"label": "Orders", "icon": "box", "perm": "view_orders", "children": [
        {"label": "Alle orders", "endpoint": "planning.orders", "icon": "list", "perm": "view_orders"},
        {"label": "Belangrijke orders", "endpoint": "planning.important_orders", "icon": "star", "perm": "view_orders"}]},
    {"label": "Magazijn", "icon": "warehouse", "perm": "view_preassembly", "children": [
        {"label": "Live magazijnstatus", "endpoint": "planning.magazijn", "icon": "warehouse", "perm": "view_magazijn"},
        {"label": "Voormonteren", "endpoint": "planning.voormonteren", "icon": "wrench", "perm": "view_preassembly"},
        {"label": "Pakbonnen", "endpoint": "planning.picklijst", "icon": "clipboard", "perm": "view_preassembly"}]},
    {"label": "Klanten", "endpoint": "planning.clients", "icon": "users", "perm": "view_orders"},
    {"label": "Documenten", "endpoint": "planning.documenten", "icon": "doc", "perm": "view_documents",
     "subs": [{"label": "Openbaar kladblok", "endpoint": "planning.kladblok", "icon": "pencil", "perm": "view_documents"},
              {"label": "Handleiding", "endpoint": "planning.handleiding", "icon": "doc", "perm": "view_documents"}]},
    {"label": "Teamchat", "endpoint": "planning.chat", "icon": "chat", "perm": "view_orders"},
    {"label": "Monteurs", "endpoint": "planning.monteurs", "icon": "idcard", "perm": "view_personnel",
     "subs": [{"label": "Urenregister", "endpoint": "planning.urenregister", "icon": "clock", "perm": "view_personnel"}]},
    {"label": "Bussen", "endpoint": "planning.busses", "icon": "truck", "perm": "view_personnel",
     "subs": [{"label": "Bus-issues", "endpoint": "planning.bus_issues", "icon": "alert", "perm": "view_personnel"}]},
    {"label": "Rapportages", "icon": "chart", "perm": None, "children": [
        {"label": "Monteursprestaties", "endpoint": "planning.performance", "icon": "chart", "perm": "view_performance"},
        {"label": "Kilometers", "endpoint": "planning.vehicle_km", "icon": "truck", "perm": "view_reports"},
        {"label": "Handtekeningen", "endpoint": "planning.signatures", "icon": "pencil", "perm": "view_signatures"}]},
    {"label": "Vrije dagen", "endpoint": "planning.free_days", "icon": "sun", "perm": "manage_freedays"},
    {"label": "Instellingen", "icon": "gear", "perm": None, "children": [
        {"label": "Live status koppelingen", "endpoint": "planning.koppelingen", "icon": "link", "perm": "view_connections"},
        {"label": "Koppelingen instellen", "endpoint": "planning.integrations", "icon": "gear", "perm": "manage_integrations"},
        {"label": "Automatische e-mails", "endpoint": "planning.email_templates", "icon": "mail", "perm": "manage_settings"},
        {"label": "Artikelen", "endpoint": "planning.products", "icon": "box", "perm": "manage_settings"},
        {"label": "Gebruikers", "endpoint": "planning.users", "icon": "users", "perm": "manage_users"}]},
]


def _today_iso():
    return datetime.now().date().isoformat()


def _shift_iso(iso, days):
    """ISO-datum N dagen verschuiven; valt terug op vandaag bij onleesbare invoer."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except Exception:
        d = datetime.now().date()
    return (d + timedelta(days=days)).isoformat()


def _items_by_order(conn, order_ids):
    """Dict order_id -> 'Nx artikel, Nx artikel'."""
    if not order_ids:
        return {}
    qmarks = ",".join("?" * len(order_ids))
    rows = conn.execute(f"SELECT order_id, qty, name FROM order_items WHERE order_id IN ({qmarks})",
                        list(order_ids)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["order_id"], []).append(f"{r['qty']}x {r['name']}")
    return {k: ", ".join(v) for k, v in out.items()}


def eta_back_to_base(lat, lng, remaining_stops, speed):
    """Schat hoe laat de monteur terug is in Breda om te laden."""
    km = haversine((lat, lng), BREDA)
    speed_factor = 1.0 + (3 - (speed or 3)) * 0.08      # snellere monteur = sneller klaar
    drive_min = km / 45 * 60
    work_min = remaining_stops * 22 * speed_factor
    eta = datetime.now() + timedelta(minutes=drive_min + work_min)
    return eta.strftime("%H:%M"), round(km)


def _gt_today(gt):
    """Geplande tijd 'HH:MM' als datetime van vandaag (of None)."""
    if not gt:
        return None
    try:
        h, m = map(int, gt.split(":"))
        return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    except Exception:
        return None


def compute_arrivals(stops, monteur, live, is_today):
    """Live aankomsttijden (AT) per stop + ETA terug in Breda, o.b.v. GPS + route.

    stops: lijst met city, montage_min, status, slot_start (op volgorde).
    Geeft (arrivals, eta_back). arrivals[i] = {at, status, delta}.
    status: done | ontime | late | none(niet live)."""
    speed = monteur["speed"] or 3
    mfac = 1.0 + (3 - speed) * 0.08
    arrivals = []
    eta_back = None
    if live and is_today:
        pos, t = live, datetime.now()
        for s in stops:
            if s["status"] == "afgerond":
                arrivals.append({"at": None, "status": "done", "delta": None})
                continue
            city = CITY_COORDS.get(s["city"], BREDA)
            t = t + timedelta(minutes=haversine(pos, city) / 45 * 60)
            gtdt = _gt_today(s["slot_start"])
            delta = round((t - gtdt).total_seconds() / 60) if gtdt else None
            arrivals.append({"at": t.strftime("%H:%M"),
                             "status": ("late" if (delta or 0) > 5 else "ontime"),
                             "delta": delta})
            t = t + timedelta(minutes=(s["montage_min"] or 0) * mfac)
            pos = city
        eta_back = (t + timedelta(minutes=haversine(pos, BREDA) / 45 * 60)).strftime("%H:%M")
    else:
        for s in stops:
            arrivals.append({"at": None, "status": "none", "delta": None})
        if stops:
            last = stops[-1]
            base = _gt_today(last["slot_start"])
            if base:
                lastcity = CITY_COORDS.get(last["city"], BREDA)
                eta = base + timedelta(minutes=(last["montage_min"] or 0) * mfac
                                       + haversine(lastcity, BREDA) / 45 * 60)
                eta_back = eta.strftime("%H:%M")
    return arrivals, eta_back


LIVE_MAX_MIN = 30   # locatie geldt als 'live' zolang de laatste update < 30 min oud is


def _live_cutoff():
    return (datetime.now() - timedelta(minutes=LIVE_MAX_MIN)).isoformat(timespec="minutes")


def _live_loc(conn, mid):
    r = conn.execute("SELECT lat,lng,live,updated_at FROM monteur_location WHERE monteur_id=?", (mid,)).fetchone()
    if r and r["live"] and (r["updated_at"] or "") >= _live_cutoff():
        return (r["lat"], r["lng"])
    return None


def route_alerts(monteur_id, has_stops):
    """Significante files/werkzaamheden op de route (>= ALERT_THRESHOLD min).
    Stub — wordt live gevoed door de route-/verkeerskoppeling. Onder de drempel = vrije route."""
    out = []
    if has_stops and monteur_id == 1:        # demo: één route met een echte file
        out.append({"icon": "🚗", "desc": "File A2 richting Den Bosch", "min": 25})
        out.append({"icon": "🚧", "desc": "Wegwerkzaamheden N65 (Tilburg)", "min": 10})
    return [a for a in out if a["min"] >= ALERT_THRESHOLD]


# --------------------------------------------------------------------------- #
#  Auth-routes
# --------------------------------------------------------------------------- #
@bp.route("/")
def home():
    u = current_user()
    if not u:
        return redirect(url_for("planning.login"))
    return redirect(url_for("planning.monteur_app") if u["role"] == "monteur" else url_for("planning.dashboard"))


def _office_demo_accounts():
    conn = db()
    rows = conn.execute("SELECT name, email, role FROM users WHERE role!='monteur' AND active=1 ORDER BY id").fetchall()
    conn.close()
    return [{"name": r["name"], "email": r["email"], "role": ROLE_LABELS.get(r["role"], r["role"])} for r in rows]


def _email_cfg():
    conn = db()
    cfg = {r["field"]: r["value"] for r in
           conn.execute("SELECT field,value FROM integrations WHERE ikey=?", ("email",)).fetchall()}
    conn.close()
    return cfg


def _email_configured():
    c = _email_cfg()
    from_email = (c.get("from_email") or c.get("smtp_user") or "").strip()
    has_resend = bool((c.get("resend_api_key") or "").strip() and from_email)
    has_smtp = bool((c.get("smtp_host") or "").strip() and (c.get("smtp_user") or "").strip()
                    and (c.get("smtp_pass") or "").strip())
    return has_resend or has_smtp


def _mail_live():
    """Echt versturen alleen als de mailbox is ingesteld ÉN 'E-mails écht versturen' aanstaat."""
    return _email_configured() and (_mailcfg_send_live())


def _mailcfg_send_live():
    return (_email_cfg().get("send_live") or "0") == "1"


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _twofa_email_html(name, code, subtitle="Planning"):
    html = """<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#e7ebe7;padding:24px 0;margin:0;">
<tr><td align="center">
<table role="presentation" width="460" cellpadding="0" cellspacing="0" style="max-width:460px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e6ebe4;font-family:Arial,Helvetica,sans-serif;">
<tr><td style="background:#0f3d3e;padding:22px 24px;">
<span style="color:#ffffff;font-size:18px;font-weight:bold;letter-spacing:2px;">OfficeRoute</span>
<span style="color:#cda35a;font-size:12px;"> &middot; __SUB__</span></td></tr>
<tr><td style="padding:26px 24px 6px;">
<p style="margin:0 0 4px;font-size:16px;color:#16302d;">Hoi __NAME__,</p>
<p style="margin:0 0 20px;font-size:15px;color:#5f6b64;line-height:1.6;">Hier is je inlogcode voor OfficeRoute. Vul 'm in op het inlogscherm om verder te gaan.</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef3ec;border:1px solid #cda35a;border-radius:12px;margin-bottom:16px;">
<tr><td align="center" style="padding:18px;">
<div style="font-size:12px;color:#5f6b64;margin-bottom:8px;">Je verificatiecode</div>
<div style="font-size:38px;font-weight:bold;color:#0f3d3e;letter-spacing:8px;">__CODE__</div></td></tr></table>
<p style="margin:0 0 18px;font-size:13px;color:#5f6b64;">Deze code is 5 minuten geldig.</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7efe0;border:1px solid #e3c98f;border-radius:12px;">
<tr><td style="padding:14px 15px;font-size:13px;color:#6b4e15;line-height:1.55;">
<b>Niet zelf ingelogd?</b> Heb je g&eacute;&eacute;n inlogpoging gedaan, neem dan <b>direct contact op met de beheerder</b>. Deel deze code met niemand.</td></tr></table>
</td></tr>
<tr><td style="padding:18px 24px 22px;border-top:1px solid #e6ebe4;">
<p style="margin:0;font-size:11px;color:#97a39d;line-height:1.6;">Deze e-mail is automatisch verstuurd door OfficeRoute &middot; Office-Interior.<br>Antwoorden op dit bericht worden niet gelezen.</p></td></tr>
</table></td></tr></table>"""
    return html.replace("__SUB__", _esc(subtitle)).replace("__NAME__", _esc(name) or "collega").replace("__CODE__", _esc(code))


def _api_send(to, subject, text, html=None):
    """Verstuur via Resend HTTPS-API (werkt op Render); anders SMTP (lokaal). Respecteert testmodus."""
    recips = [r for r in (to if isinstance(to, list) else [to]) if r]
    if not recips:
        return False
    c = _email_cfg()
    if (c.get("send_live") or "0") != "1":
        return False   # testmodus: niets echt versturen
    from_email = (c.get("from_email") or c.get("smtp_user") or "").strip()
    if not from_email:
        return False
    frm = "%s <%s>" % ((c.get("from_name") or "Office-Interior").strip(), from_email)
    reply_to = (c.get("reply_to") or "").strip()
    key = (c.get("resend_api_key") or "").strip()
    if key:
        try:
            body = {"from": frm, "to": recips, "subject": subject, "text": text, "html": html or text}
            if reply_to:
                body["reply_to"] = reply_to
            payload = json.dumps(body).encode("utf-8")
            req = urllib.request.Request("https://api.resend.com/emails", data=payload,
                                         headers={"Authorization": "Bearer " + key,
                                                  "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            return True
        except Exception:
            return False
    host = (c.get("smtp_host") or "").strip()
    user = (c.get("smtp_user") or "").strip()
    pwd = (c.get("smtp_pass") or "").strip()
    if not (host and user and pwd):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = ", ".join(recips)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(host, int(c.get("smtp_port") or 587), timeout=10) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        return True
    except Exception:
        return False


def _send_2fa_email(to_email, code, name=""):
    """Mail de 6-cijferige inlogcode (HTML + tekst). True bij succes, anders False (val terug op scherm)."""
    text = ("Hoi %s,\n\nJe verificatiecode voor OfficeRoute is: %s\n\n"
            "De code is 5 minuten geldig.\n\n"
            "Niet zelf ingelogd? Neem dan direct contact op met de beheerder en deel "
            "deze code met niemand.\n" % (name or "collega", code))
    return _api_send(to_email, "Je OfficeRoute-inlogcode: %s" % code, text, _twofa_email_html(name, code))


def _send_mail(to_email, subject, body, html_body=None):
    """Centrale verzendlaag voor alle uitgaande klantmail (Resend/HTTPS, SMTP-fallback, testmodus-gate)."""
    return _api_send(to_email, subject, body, html_body)


_NL_DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
_NL_MONTHS = ["", "januari", "februari", "maart", "april", "mei", "juni", "juli",
              "augustus", "september", "oktober", "november", "december"]


def _nl_date(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return "%s %d %s" % (_NL_DAYS[d.weekday()], d.day, _NL_MONTHS[d.month])
    except Exception:
        return iso or ""


def _wide_window(slot_start, slot_end):
    """Ruim tijdvak voor de klant (zodat we niet teleurstellen als we later zijn)."""
    def hh(s):
        try:
            return int((s or "").split(":")[0])
        except Exception:
            return None
    a, b = hh(slot_start), hh(slot_end)
    if a is None or b is None:
        return "08:00 – 17:00"
    sh = max(7, a)
    eh = min(18, max(b + 2, sh + 3))
    return "%02d:00 – %02d:00" % (sh, eh)


MAIL_TEXT_DEFAULTS = {
    "mailtxt_confirm_h": "Uw bestelling is ingepland",
    "mailtxt_confirm_b": "Goed nieuws, uw bestelling is ingepland. Hieronder vindt u het geplande bezorgmoment.",
    "mailtxt_today_h": "Wij komen vandaag langs",
    "mailtxt_today_b": ("Vandaag bezorgen wij uw bestelling. Gekozen voor montage? Dan doen we dat natuurlijk ook! "
                        "Hieronder vindt u de details. Onze monteur stuurt onderweg nog een bericht zodra hij naar u toe komt.\n\n"
                        "Wilt u iets aan de chauffeur doorgeven (bijv. \"bel doet het niet\")? Dat kan via de knop hieronder."),
    "mailtxt_near_h": "Onze monteur is er bijna",
    "mailtxt_near_b": "Onze monteur is er bijna. U kunt hem live volgen via de knop hieronder.",
    "mailtxt_delay_h": "Update over uw levertijd",
    "mailtxt_delay_b": "Door omstandigheden onderweg is de verwachte aankomsttijd iets opgeschoven. Onze excuses voor het ongemak.",
}


def _mailtxt(key):
    """Bewerkbare mailtekst uit settings, anders de standaardtekst."""
    conn = db()
    r = conn.execute("SELECT value FROM settings WHERE skey=?", (key,)).fetchone()
    conn.close()
    v = r["value"] if r else None
    return v if (v is not None and v.strip()) else MAIL_TEXT_DEFAULTS.get(key, "")


def _paras(greet, bodytext):
    return [greet] + [p for p in (bodytext or "").split("\n\n") if p.strip()]


def _purge_old_customer_notes(conn):
    """Privacy: klantopmerkingen wissen 12 uur na de levering (fulfilled_at)."""
    try:
        cutoff = (datetime.now() - timedelta(hours=12)).isoformat(timespec="minutes")
        conn.execute("UPDATE orders SET customer_note=NULL WHERE customer_note IS NOT NULL "
                     "AND fulfilled=1 AND fulfilled_at IS NOT NULL AND fulfilled_at < ?", (cutoff,))
        conn.commit()
    except Exception:
        pass


def _brand_email(heading, paragraphs, info=None, button=None, note=None):
    """Nette HTML-klantmail in de OFFICE-INTERIOR-huisstijl (teal/goud).
    paragraphs: tekstalinea's; info: (label, waarde)-rijen; button: (tekst, url); note: melding-blok."""
    paras = ""
    for p in (paragraphs or []):
        if p:
            paras += ('<p style="margin:0 0 14px;font-size:15px;color:#3a4a45;line-height:1.65;">'
                      + _esc(p).replace("\n", "<br>") + '</p>')
    info_html = ""
    if info:
        rows = ""
        for label, value in info:
            rows += ('<tr><td style="padding:7px 2px;font-size:13px;color:#6b7a74;">' + _esc(label) + '</td>'
                     '<td style="padding:7px 2px;font-size:14px;color:#16302d;font-weight:bold;text-align:right;">'
                     + _esc(value) + '</td></tr>')
        info_html = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                     'style="background:#eef3ec;border:1px solid #cfe0d7;border-radius:12px;margin:4px 0 16px;">'
                     '<tr><td style="padding:8px 16px;"><table role="presentation" width="100%" cellpadding="0" '
                     'cellspacing="0">' + rows + '</table></td></tr></table>')
    note_html = ""
    if note:
        note_html = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                     'style="background:#f7efe0;border:1px solid #e3c98f;border-radius:12px;margin:0 0 16px;">'
                     '<tr><td style="padding:12px 14px;font-size:13px;color:#6b4e15;line-height:1.55;">'
                     + _esc(note) + '</td></tr></table>')
    btn_html = ""
    if button:
        btn_html = ('<table role="presentation" cellpadding="0" cellspacing="0" style="margin:2px 0 16px;">'
                    '<tr><td style="background:#0f3d3e;border-radius:10px;">'
                    '<a href="' + _esc(button[1]) + '" style="display:inline-block;padding:12px 22px;color:#ffffff;'
                    'font-size:14px;font-weight:bold;text-decoration:none;">' + _esc(button[0]) + '</a></td></tr></table>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="background:#f1ede5;padding:24px 0;margin:0;"><tr><td align="center">'
            '<table role="presentation" width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;'
            'background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e6ebe4;font-family:Arial,Helvetica,sans-serif;">'
            '<tr><td style="background:#0f3d3e;padding:18px 28px;">'
            '<span style="color:#ffffff;font-size:18px;font-weight:bold;letter-spacing:2px;">OFFICE-INTERIOR</span></td></tr>'
            '<tr><td style="height:3px;background:#cda35a;"></td></tr>'
            '<tr><td style="padding:26px 28px 4px;">'
            '<h1 style="margin:0 0 14px;font-size:20px;color:#0f3d3e;font-weight:bold;">' + _esc(heading) + '</h1>'
            + paras + info_html + note_html + btn_html +
            '</td></tr><tr><td style="padding:6px 28px 22px;">'
            '<p style="margin:10px 0 0;padding-top:14px;border-top:1px solid #eef0ec;font-size:12px;color:#8a948f;'
            'line-height:1.6;">Vragen? Mail planning@office-interior.com of bel 085-0481444.</p></td></tr>'
            '</table></td></tr></table>')


def _planning_confirmation_mail(client, date_iso, slot_start, slot_end, order_number):
    """Bevestiging die automatisch ná het inplannen gaat (ruim tijdvak, geen live ETA)."""
    greet = "Beste %s," % (client or "klant")
    intro = _mailtxt("mailtxt_confirm_b")
    subject = "Uw bestelling is ingepland #%s" % order_number
    tijd = _wide_window(slot_start, slot_end)
    body = "%s\n\n%s\n\nBezorgdatum: %s\nVerwachte tijd: %s\nOrdernummer: #%s" % (
        greet, intro, _nl_date(date_iso), tijd, order_number)
    html = _brand_email(_mailtxt("mailtxt_confirm_h"), _paras(greet, intro),
                        info=[("Bezorgdatum", _nl_date(date_iso)), ("Verwachte tijd", tijd),
                              ("Ordernummer", "#" + str(order_number))],
                        note="Op de dag zelf ontvangt u een mail met een live volglink en de verwachte aankomsttijd van de monteur.")
    return subject, body, html


def _finish_login(u, nxt):
    session["p_user_id"] = u["id"]
    session.pop("twofa", None)
    if u["role"] == "monteur":
        return redirect(url_for("planning.monteur_app"))
    return redirect(nxt or url_for("planning.dashboard"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    show_2fa = False
    demo_code = None
    twofa_email = None
    code_sent = False
    if request.method == "POST":
        if request.form.get("twofa_code") is not None:
            # Stap 2: 2FA-code verifiëren
            tf = session.get("twofa") or {}
            code = (request.form.get("twofa_code") or "").strip()
            if not tf:
                error = "Sessie verlopen. Log opnieuw in."
            elif time.time() > tf.get("exp", 0):
                session.pop("twofa", None)
                error = "Code verlopen. Log opnieuw in."
            elif code == tf.get("code"):
                conn = db()
                u = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (tf["uid"],)).fetchone()
                conn.close()
                if u:
                    return _finish_login(u, tf.get("next"))
                error = "Account niet gevonden."
            else:
                error = "Onjuiste 2FA-code."
                show_2fa = True
                twofa_email = tf.get("email")
                if tf.get("sent"):
                    code_sent = True
                else:
                    demo_code = tf.get("code")
        else:
            # Stap 1: e-mail + wachtwoord
            email = (request.form.get("email") or "").strip().lower()
            pw = request.form.get("password") or ""
            conn = db()
            u = conn.execute("SELECT * FROM users WHERE lower(email)=? AND active=1", (email,)).fetchone()
            conn.close()
            if u and check_password_hash(u["password"], pw):
                # Oude/zware hash (scrypt of pbkdf2 met hoge telling) -> eenmalig
                # omzetten naar de lichte methode, zodat elke volgende login snel is.
                if not (u["password"] or "").startswith(_PW_METHOD):
                    try:
                        cu = db()
                        cu.execute("UPDATE users SET password=? WHERE id=?", (_hash_pw(pw), u["id"]))
                        cu.commit(); cu.close()
                    except Exception:
                        pass
                code = "%06d" % secrets.randbelow(1000000)
                show_2fa = True
                twofa_email = u["email"]
                # Code alleen als 'verstuurd' beschouwen als de mail ECHT gelukt is
                # (synchroon; Resend faalt snel bij een niet-gekoppeld domein). Zo raakt
                # niemand buitengesloten: mislukt de mail, dan verschijnt de code als
                # terugval op het scherm (alleen zichtbaar NA een juist wachtwoord).
                if _mail_live():
                    code_sent = _send_2fa_email(u["email"], code, u["name"])
                if not code_sent:
                    demo_code = code
                session["twofa"] = {"uid": u["id"], "code": code, "exp": time.time() + 300,
                                    "next": request.args.get("next"), "email": u["email"], "sent": code_sent}
            else:
                error = "Onjuiste inloggegevens."
    return render_template("planning/login.html", error=error, show_2fa=show_2fa,
                           demo_code=demo_code, twofa_email=twofa_email, code_sent=code_sent,
                           office_accounts=_office_demo_accounts())


@bp.route("/logout")
def logout():
    session.pop("p_user_id", None)
    session.pop("twofa", None)
    return redirect(url_for("planning.login"))


# --------------------------------------------------------------------------- #
#  PWA: manifest + service worker (monteur-app installeerbaar + offline)
# --------------------------------------------------------------------------- #
@bp.route("/manifest.webmanifest")
def manifest():
    icon = url_for("static", filename="icon.svg")
    data = {
        "name": "OfficeRoute", "short_name": "OfficeRoute",
        "description": "Routes en leveringen voor monteurs",
        "start_url": "/monteur", "scope": "/", "display": "standalone",
        "background_color": "#0f3d3e", "theme_color": "#0f3d3e",
        "icons": [{"src": icon, "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
    }
    return Response(json.dumps(data), mimetype="application/manifest+json")


@bp.route("/sw.js")
def service_worker():
    js = """
const C='officeroute-v1';
self.addEventListener('install', e=>{ self.skipWaiting(); });
self.addEventListener('activate', e=>{ self.clients.claim(); });
self.addEventListener('fetch', e=>{
  const u=new URL(e.request.url);
  if(e.request.method!=='GET' || u.origin!==location.origin) return;
  e.respondWith(
    fetch(e.request).then(r=>{ const cp=r.clone(); caches.open(C).then(c=>c.put(e.request,cp)); return r; })
      .catch(()=> caches.match(e.request).then(m=> m || caches.match('/monteur')))
  );
});
"""
    return Response(js, mimetype="application/javascript",
                    headers={"Service-Worker-Allowed": "/"})


# --------------------------------------------------------------------------- #
#  Dashboard
# --------------------------------------------------------------------------- #
@bp.route("/version")
def version():
    """Publieke versie-/health-check (geen login). Door keep-alive aangeroepen."""
    return jsonify(v=APP_VERSION)


_LAST_AUTO_MAIL = 0.0  # throttle: mailbatch hoogstens 1x per 10 min, niet elke dashboard-load


def _auto_send_bg():
    try:
        auto_send_daily_mails()
    except Exception:
        pass


@bp.route("/dashboard")
def dashboard():
    guard = login_required("view_planning")
    if guard:
        return guard
    u = current_user()
    auto_mails = 0
    if has_perm("inform_clients") or u["role"] == "beheerder":
        global _LAST_AUTO_MAIL
        if time.time() - _LAST_AUTO_MAIL > 600:
            _LAST_AUTO_MAIL = time.time()
            # Op de achtergrond: dashboard wacht niet op het versturen van mails.
            threading.Thread(target=_auto_send_bg, daemon=True).start()
    conn = db()
    _purge_old_customer_notes(conn)
    today = _today_iso()
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    week_end = (datetime.now().date() + timedelta(days=7)).isoformat()

    def scalar(q, a=()):
        return conn.execute(q, a).fetchone()[0]

    stats = {
        "tomorrow": scalar("SELECT COUNT(*) FROM planning WHERE date=?", (tomorrow,)),
        "week": scalar("SELECT COUNT(*) FROM planning WHERE date>=? AND date<=?", (today, week_end)),
        "unplanned": scalar("SELECT COUNT(*) FROM orders WHERE status='in_te_plannen'"),
        "open_orders": scalar("SELECT COUNT(*) FROM orders WHERE status IN('in_te_plannen','gepland')"),
        "underway": scalar("SELECT COUNT(*) FROM planning WHERE status='onderweg'"),
        "monteurs_active": scalar("SELECT COUNT(DISTINCT monteur_id) FROM planning "
                                  "WHERE date=? AND monteur_id IS NOT NULL AND status!='afgerond'", (today,)),
        "drafts_blocked": scalar("SELECT COUNT(*) FROM orders WHERE is_draft=1"),
        "important": scalar("SELECT COUNT(*) FROM orders WHERE amount>=? AND is_draft=0 AND desired_date>=?",
                            (IMPORTANT_THRESHOLD, today)),
    }
    # monteurs onderweg + ETA terug in Breda
    underway = []
    rows = conn.execute("""
        SELECT m.id, m.name, m.color, m.speed, l.lat, l.lng, l.updated_at
        FROM monteurs m JOIN monteur_location l ON l.monteur_id=m.id
        WHERE m.active=1 AND l.live=1 AND l.updated_at >= ?""", (_live_cutoff(),)).fetchall()
    for r in rows:
        remaining = conn.execute("""SELECT COUNT(*) FROM planning WHERE monteur_id=? AND date=? AND status!='afgerond'""",
                                 (r["id"], today)).fetchone()[0]
        eta, km = eta_back_to_base(r["lat"], r["lng"], remaining, r["speed"])
        # eerstvolgende stop = waar hij nu naartoe onderweg is
        nxt = conn.execute("""SELECT c.name AS client FROM planning p
                              JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
                              WHERE p.monteur_id=? AND p.date=? AND p.status!='afgerond'
                              ORDER BY p.sequence LIMIT 1""", (r["id"], today)).fetchone()
        underway.append({"name": r["name"], "color": r["color"], "eta_base": eta, "km_base": km,
                         "next_client": (nxt["client"] if nxt else None),
                         "stops_after": max(0, remaining - 1), "updated": r["updated_at"]})

    unplanned_all = conn.execute("""SELECT o.*, c.name AS client,
                                     (SELECT GROUP_CONCAT(qty || 'x ' || name, ', ') FROM order_items WHERE order_id=o.id) AS items
                                     FROM orders o LEFT JOIN clients c ON c.id=o.client_id
                                     WHERE o.status='in_te_plannen' ORDER BY o.desired_date""").fetchall()
    unplanned = unplanned_all[:3]
    monteurs = conn.execute("SELECT id,name FROM monteurs WHERE active=1 ORDER BY name").fetchall()

    # kantoorbezetting (dag selecteerbaar) — vaste kantoorploeg
    office_day = request.args.get("office_day", today)
    od = {r["person"]: r for r in conn.execute("SELECT * FROM office_days WHERE date=?", (office_day,)).fetchall()}
    office = []
    for name in OFFICE_STAFF:
        rec = od.get(name)
        office.append({"person": name, "status": (rec["status"] if rec else "kantoor"),
                       "note": (rec["note"] if rec else "")})

    # team-vragen (@mentions) aan mij
    my_questions = conn.execute("""
        SELECT q.*, uf.name AS from_name, o.order_number FROM team_questions q
        LEFT JOIN users uf ON uf.id=q.from_user_id LEFT JOIN orders o ON o.id=q.order_id
        WHERE q.to_user_id=? AND q.resolved=0 ORDER BY q.ts DESC""", (u["id"],)).fetchall()
    all_users = conn.execute("SELECT id,name FROM users WHERE active=1 AND role NOT IN('monteur','picker') AND id!=? ORDER BY name",
                             (u["id"],)).fetchall()
    conn.close()
    return render_template("planning/dashboard.html", stats=stats, underway=underway, unplanned=unplanned,
                           unplanned_all=unplanned_all, monteurs=monteurs,
                           office=office, office_day=office_day, today=today, auto_mails=auto_mails,
                           my_questions=my_questions, all_users=all_users,
                           maatwerk_orders=_orders_needing_custom())


@bp.route("/api/locations")
def api_locations():
    if not current_user():
        return jsonify([]), 403
    today = _today_iso()
    conn = db()
    rows = conn.execute("""SELECT m.id, m.name, m.color, m.speed, l.lat, l.lng, l.updated_at
                           FROM monteurs m JOIN monteur_location l ON l.monteur_id=m.id
                           WHERE m.active=1 AND l.live=1 AND l.updated_at >= ?""", (_live_cutoff(),)).fetchall()
    out = []
    for r in rows:
        remaining = conn.execute("SELECT COUNT(*) FROM planning WHERE monteur_id=? AND date=? AND status!='afgerond'",
                                 (r["id"], today)).fetchone()[0]
        eta, km = eta_back_to_base(r["lat"], r["lng"], remaining, r["speed"])
        out.append({"id": r["id"], "name": r["name"], "color": r["color"],
                    "lat": r["lat"], "lng": r["lng"], "eta_base": eta, "km_base": km,
                    "updated": r["updated_at"]})
    conn.close()
    return jsonify(out)


@bp.route("/api/location", methods=["POST"])
def api_location():
    """De monteur-app pusht hier de live GPS-positie van de telefoon naartoe."""
    u = current_user()
    if not u or not u["monteur_id"] or not has_perm("monteur_app"):
        return jsonify(ok=False, error="Geen monteur"), 403
    data = request.get_json(silent=True) or {}
    try:
        lat, lng = float(data["lat"]), float(data["lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="Ongeldige locatie."), 400
    live = 1 if data.get("live", True) else 0
    conn = db()
    conn.execute("""INSERT INTO monteur_location(monteur_id,lat,lng,updated_at,live) VALUES(?,?,?,?,?)
                    ON CONFLICT(monteur_id) DO UPDATE SET lat=excluded.lat,lng=excluded.lng,
                    updated_at=excluded.updated_at,live=excluded.live""",
                 (u["monteur_id"], lat, lng, datetime.now().isoformat(timespec="minutes"), live))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/office", methods=["POST"])
def api_office():
    if not has_perm("view_planning"):
        return jsonify(ok=False), 403
    data = request.get_json(force=True)
    conn = db()
    conn.execute("""INSERT INTO office_days(person,date,status,note) VALUES(?,?,?,?)
                    ON CONFLICT(person,date) DO UPDATE SET status=excluded.status,note=excluded.note""",
                 (data["person"], data["date"], data["status"], data.get("note", "")))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/question", methods=["POST"])
def api_question():
    u = current_user()
    if not u:
        return jsonify(ok=False), 403
    text = (request.form.get("text") or "").strip()
    to_user = request.form.get("to_user_id")
    order_num = (request.form.get("order_number") or "").strip().lstrip("#")
    if text and to_user:
        conn = db()
        oid = None
        if order_num:
            row = conn.execute("SELECT id FROM orders WHERE order_number=?", (order_num,)).fetchone()
            oid = row["id"] if row else None
        conn.execute("""INSERT INTO team_questions(from_user_id,to_user_id,order_id,text,ts,resolved)
                        VALUES(?,?,?,?,?,0)""",
                     (u["id"], to_user, oid, text, datetime.now().isoformat(timespec="minutes")))
        conn.commit()
        conn.close()
        flash("Vraag verstuurd.")
    return redirect(request.referrer or url_for("planning.dashboard"))


@bp.route("/api/question/resolve/<int:qid>", methods=["POST"])
def api_question_resolve(qid):
    u = current_user()
    if not u:
        return jsonify(ok=False), 403
    conn = db()
    conn.execute("UPDATE team_questions SET resolved=1 WHERE id=? AND to_user_id=?", (qid, u["id"]))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("planning.dashboard"))


# --------------------------------------------------------------------------- #
#  Planning (dagweergave in routeblokken, zoals het vertrouwde overzicht)
# --------------------------------------------------------------------------- #
def _ensure_roster(conn, day, monteurs):
    """Zet, als er nog geen roster voor deze dag is, de standaard-monteurs klaar
    zodat toevoegen/verwijderen daarna expliciet per dag werkt."""
    if not conn.execute("SELECT 1 FROM day_roster WHERE date=? LIMIT 1", (day,)).fetchone():
        for m in monteurs:
            if m["standard"]:
                conn.execute("INSERT OR IGNORE INTO day_roster(date,monteur_id) VALUES(?,?)", (day, m["id"]))


def _day_roster_ids(conn, day, monteurs, job_mids):
    """Welke monteurs verschijnen op deze dag: het dag-roster (of standaard-monteurs
    als er nog geen roster is) plus iedereen die al stops heeft."""
    rows = conn.execute("SELECT monteur_id FROM day_roster WHERE date=?", (day,)).fetchall()
    base = {r["monteur_id"] for r in rows} if rows else {m["id"] for m in monteurs if m["standard"]}
    return base | set(job_mids)


@bp.route("/planning/roster/add", methods=["POST"])
def roster_add():
    guard = login_required("edit_planning")
    if guard:
        return guard
    day = request.form.get("day") or _today_iso()
    mid = request.form.get("monteur_id")
    if mid:
        conn = db()
        mons = conn.execute("SELECT id,standard FROM monteurs WHERE active=1").fetchall()
        _ensure_roster(conn, day, mons)
        conn.execute("INSERT OR IGNORE INTO day_roster(date,monteur_id) VALUES(?,?)", (day, mid))
        conn.commit()
        conn.close()
        flash("Monteur toegevoegd aan deze dag.")
    return redirect(url_for("planning.planning", day=day))


@bp.route("/planning/roster/remove", methods=["POST"])
def roster_remove():
    guard = login_required("edit_planning")
    if guard:
        return guard
    day = request.form.get("day") or _today_iso()
    mid = request.form.get("monteur_id")
    if mid:
        conn = db()
        if conn.execute("SELECT 1 FROM planning WHERE date=? AND monteur_id=?", (day, mid)).fetchone():
            conn.close()
            flash("Deze monteur heeft nog stops op deze dag — sleep die er eerst af.")
            return redirect(url_for("planning.planning", day=day))
        mons = conn.execute("SELECT id,standard FROM monteurs WHERE active=1").fetchall()
        _ensure_roster(conn, day, mons)
        conn.execute("DELETE FROM day_roster WHERE date=? AND monteur_id=?", (day, mid))
        conn.commit()
        conn.close()
        flash("Monteur van deze dag gehaald.")
    return redirect(url_for("planning.planning", day=day))


@bp.route("/planning")
def planning():
    guard = login_required("view_planning")
    if guard:
        return guard
    day = request.args.get("day", _today_iso())
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        day = _today_iso()
        d = datetime.strptime(day, "%Y-%m-%d").date()
    conn = db()
    monteurs = conn.execute("SELECT * FROM monteurs WHERE active=1 ORDER BY id").fetchall()
    busmap = {b["id"]: b for b in conn.execute("SELECT id,max_weight,empty_weight FROM busses").fetchall()}
    jobs = conn.execute("""
        SELECT p.*, o.order_number, o.delivery_address, o.city, o.postal, o.email AS o_email,
               o.phone, o.notes, o.instructions, o.volume, o.weight, o.montage_min, o.amount, o.source, o.service_type,
               o.client_id, c.name AS client
        FROM planning p JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
        WHERE p.date=? ORDER BY p.monteur_id, p.sequence""", (day,)).fetchall()
    usort = request.args.get("usort", "nieuw")
    unplanned = conn.execute("""SELECT o.*, c.name AS client FROM orders o LEFT JOIN clients c ON c.id=o.client_id
                                WHERE o.status='in_te_plannen' ORDER BY """
                             + ("o.id ASC" if usort == "oud" else "o.id DESC")).fetchall()
    frees = {r["monteur_id"]: r["type"] for r in
             conn.execute("SELECT * FROM free_days WHERE date_from<=? AND date_to>=?", (day, day)).fetchall()}
    job_mids = {j["monteur_id"] for j in jobs if j["monteur_id"]}
    shown_ids = _day_roster_ids(conn, day, monteurs, job_mids)
    addable = [m for m in monteurs if m["id"] not in shown_ids]
    all_order_ids = [j["order_id"] for j in jobs] + [o["id"] for o in unplanned]
    items_map = _items_by_order(conn, all_order_ids)
    # Montagetijd (workload) per order uit de artikelcatalogus (val terug op order.montage_min).
    _prods = _load_products(conn)
    montage_map, weight_map = {}, {}
    if jobs:
        _oids = [j["order_id"] for j in jobs]
        _by_o = {}
        for r in conn.execute("SELECT order_id,name,qty,montage_custom FROM order_items WHERE order_id IN (%s)"
                              % ",".join("?" * len(_oids)), tuple(_oids)).fetchall():
            _by_o.setdefault(r["order_id"], []).append({"name": r["name"], "qty": r["qty"], "montage_custom": r["montage_custom"]})
        for j in jobs:
            montage_map[j["order_id"]] = _order_montage(_by_o.get(j["order_id"], []), _prods,
                                                        fallback=(j["montage_min"] or 0),
                                                        service_type=j["service_type"])
            weight_map[j["order_id"]] = _order_weight(_by_o.get(j["order_id"], []), _prods,
                                                      fallback=(j["weight"] or 0))

    is_today = (day == _today_iso())
    raw, totals = {}, {}
    for j in jobs:
        raw.setdefault(j["monteur_id"], []).append(j)

    routes_by_m = {}
    for m in monteurs:
        rj = raw.get(m["id"], [])
        live = _live_loc(conn, m["id"])
        arrivals, eta_back = compute_arrivals(rj, m, live, is_today)
        enriched = []
        for s, a in zip(rj, arrivals):
            d2 = dict(s)
            d2["gt"] = s["slot_start"]
            d2["at"] = a["at"]
            d2["at_status"] = a["status"]
            d2["at_delta"] = a["delta"]
            d2["important"] = (s["amount"] or 0) >= 2000
            st = s["service_type"]
            d2["ml"] = "O" if st == "ophalen" else ("L" if st == "levering" else "M")
            d2["arts"] = items_map.get(s["order_id"], "")
            enriched.append(d2)
        routes_by_m[m["id"]] = enriched
        if rj:
            montage = sum(montage_map.get(j["order_id"], j["montage_min"] or 0) for j in rj)
            coords = [(m["home_lat"], m["home_lng"])] if m["home_lat"] else [BREDA]
            for j in rj:
                coords.append(CITY_COORDS.get(j["city"], BREDA))
            coords.append(BREDA)
            km = sum(haversine(coords[i], coords[i + 1]) for i in range(len(coords) - 1))
            alerts = route_alerts(m["id"], True)
            provs = []
            for j in rj:
                p = PROVINCE.get(j["city"])
                if p and p not in provs:
                    provs.append(p)
            load = round(sum(weight_map.get(j["order_id"], j["weight"] or 0) for j in rj))
            bus = busmap.get(m["bus_id"])
            cap = round((bus["max_weight"] or 0) - (bus["empty_weight"] or 0)) if bus else 0
            over = (load - (cap + LOAD_MARGIN_KG)) if cap else 0
            totals[m["id"]] = {"stops": len(rj), "km": round(km),
                               "time": fmt_duration(montage + km / 45 * 60),
                               "region": " · ".join(provs) if provs else "—",
                               "prov_count": len(provs),
                               "load_kg": load, "cap_kg": cap,
                               "overladen": bool(cap and over > 0), "over_kg": max(0, round(over)),
                               "eta_back": eta_back, "live": bool(live),
                               "alerts": alerts, "delay": sum(a["min"] for a in alerts),
                               "on_leave": bool(frees.get(m["id"]))}
        else:
            # Geen stops → geen reistijd/km tonen (voorkomt 'spook'-reistijd)
            _bus = busmap.get(m["bus_id"])
            totals[m["id"]] = {"stops": 0, "km": 0, "time": "—", "region": "—",
                               "prov_count": 0,
                               "load_kg": 0, "cap_kg": (round((_bus["max_weight"] or 0) - (_bus["empty_weight"] or 0)) if _bus else 0),
                               "overladen": False, "over_kg": 0,
                               "eta_back": None, "live": bool(live), "alerts": [], "delay": 0,
                               "on_leave": bool(frees.get(m["id"]))}
    conn.close()

    def _workday(dd, step):
        dd += timedelta(days=step)
        while dd.weekday() >= 5:   # zaterdag/zondag overslaan
            dd += timedelta(days=step)
        return dd

    prev_day = _workday(d, -1).isoformat()
    next_day = _workday(d, 1).isoformat()
    monday = d - timedelta(days=d.weekday())
    try:
        nd = max(5, min(int(request.args.get("nd") or 5), 30))
    except Exception:
        nd = 5
    week_days, dd = [], monday
    while len(week_days) < nd:          # alleen werkdagen (ma t/m vr)
        if dd.weekday() < 5:
            week_days.append(dd)
        dd += timedelta(days=1)
    day_label = _NL_DAYS[d.weekday()].capitalize() + d.strftime(" %d-%m-%Y")
    return render_template("planning/planning.html", monteurs=monteurs, routes=routes_by_m, totals=totals,
                           unplanned=unplanned, items=items_map, frees=frees, day=day, dateobj=d,
                           prev_day=prev_day, next_day=next_day, week_days=week_days, today=_today_iso(),
                           day_label=day_label, daynames=["ma", "di", "wo", "do", "vr", "za", "zo"],
                           nd=nd, can_edit=has_perm("edit_planning"), usort=usort,
                           shown_ids=shown_ids, addable=addable,
                           maatwerk_orders=_orders_needing_custom())


@bp.route("/api/assign", methods=["POST"])
def api_assign():
    if not has_perm("edit_planning"):
        return jsonify(ok=False, error="Geen rechten"), 403
    data = request.get_json(force=True)
    oid, mid, d = int(data["order_id"]), int(data["monteur_id"]), data["date"]
    conn = db()
    m = conn.execute("SELECT bus_id FROM monteurs WHERE id=?", (mid,)).fetchone()
    bus_id = m["bus_id"] if m else None
    seq = conn.execute("SELECT COUNT(*) FROM planning WHERE monteur_id=? AND date=?", (mid, d)).fetchone()[0]
    if conn.execute("SELECT id FROM planning WHERE order_id=?", (oid,)).fetchone():
        conn.execute("UPDATE planning SET monteur_id=?,bus_id=?,date=?,sequence=? WHERE order_id=?",
                     (mid, bus_id, d, seq, oid))
    else:
        conn.execute("""INSERT INTO planning(order_id,monteur_id,bus_id,date,sequence,status)
                        VALUES(?,?,?,?,?,'gepland')""", (oid, mid, bus_id, d, seq))
    conn.execute("UPDATE orders SET status='gepland' WHERE id=?", (oid,))
    conn.commit()
    warn = None
    fd = conn.execute("SELECT type FROM free_days WHERE monteur_id=? AND date_from<=? AND date_to>=?",
                      (mid, d, d)).fetchone()
    if fd:
        mn = conn.execute("SELECT name FROM monteurs WHERE id=?", (mid,)).fetchone()
        warn = "Let op: %s heeft vrij (%s) op deze dag — toch ingepland." % (
            (mn["name"] if mn else "Deze monteur"), fd["type"])
    conn.close()
    return jsonify(ok=True, warn=warn)


@bp.route("/api/unassign", methods=["POST"])
def api_unassign():
    if not has_perm("edit_planning"):
        return jsonify(ok=False, error="Geen rechten"), 403
    oid = int(request.get_json(force=True)["order_id"])
    conn = db()
    conn.execute("DELETE FROM planning WHERE order_id=?", (oid,))
    conn.execute("UPDATE orders SET status='in_te_plannen' WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/order-note", methods=["POST"])
def api_order_note():
    """Interne opmerking (alleen planning) en opmerking voor de monteur (zichtbaar in app)."""
    if not has_perm("edit_planning"):
        return jsonify(ok=False, error="Geen rechten"), 403
    data = request.get_json(force=True)
    oid = int(data["order_id"])
    conn = db()
    conn.execute("UPDATE orders SET notes=?, instructions=? WHERE id=?",
                 (data.get("internal", "").strip(), data.get("monteur", "").strip(), oid))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/confirm", methods=["POST"])
def api_confirm():
    if not has_perm("edit_planning"):
        return jsonify(ok=False, error="Geen rechten"), 403
    data = request.get_json(force=True)
    oid, val = int(data["order_id"]), 1 if data.get("confirmed") else 0
    conn = db()
    conn.execute("UPDATE planning SET confirmed=? WHERE order_id=?", (val, oid))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


def _home_city(monteur):
    addr = monteur["home_address"] or ""
    return addr.split(",")[-1].strip() if addr else ""


@bp.route("/api/autoplan", methods=["POST"])
def api_autoplan():
    """Plan openstaande (Shopify-)orders automatisch op de best passende dag/monteur
    qua regio-route: orders in dezelfde provincie komen bij dezelfde monteur op dezelfde dag."""
    if not has_perm("plan_orders"):
        return jsonify(ok=False, error="Geen rechten"), 403
    conn = db()
    today = datetime.now().date()
    days = []
    d = today
    while len(days) < 6:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    monteurs = conn.execute("SELECT * FROM monteurs WHERE active=1").fetchall()
    frees = conn.execute("SELECT monteur_id,date_from,date_to FROM free_days").fetchall()

    def is_free(mid, diso):
        return any(f["monteur_id"] == mid and f["date_from"] <= diso <= f["date_to"] for f in frees)

    prods = _load_products(conn)
    WORKDAY_MIN = 420          # 7 uur werk: Breda -> alle stops -> terug Breda (pauzes niet meegerekend)
    SPEED_KMH = 45.0

    def _coord(city):
        return CITY_COORDS.get(city, BREDA)

    def _route_min(stop_coords, montage_sum):
        # Reistijd Breda(Nikkelstraat) -> stops (in volgorde) -> terug Breda + totale montagetijd.
        pts = [BREDA] + stop_coords + [BREDA]
        km = sum(haversine(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        return km / SPEED_KMH * 60 + montage_sum

    def _mw_map(oids):
        # {order_id: (montagetijd_min, gewicht_kg)}
        oids = [x for x in oids if x]
        if not oids:
            return {}
        ph = ",".join(["?"] * len(oids))
        rows = conn.execute("SELECT oi.order_id AS oid, oi.name AS name, oi.qty AS qty, oi.montage_custom AS mc, "
                            "o.service_type AS svc, o.weight AS ow FROM order_items oi JOIN orders o ON o.id=oi.order_id "
                            "WHERE oi.order_id IN (%s)" % ph, tuple(oids)).fetchall()
        items, svc, ow = {}, {}, {}
        for r in rows:
            items.setdefault(r["oid"], []).append({"name": r["name"], "qty": r["qty"], "montage_custom": r["mc"]})
            svc[r["oid"]] = r["svc"]; ow[r["oid"]] = r["ow"]
        return {oid: (_order_montage(items.get(oid, []), prods, fallback=0, service_type=svc.get(oid, "montage")),
                      _order_weight(items.get(oid, []), prods, fallback=(ow.get(oid) or 0)))
                for oid in oids}

    orders = conn.execute("SELECT * FROM orders WHERE status='in_te_plannen' AND is_draft=0 ORDER BY desired_date").fetchall()

    # Bestaande stops per (monteur, dag): coord + provincie + montagetijd (1x inladen).
    routes_state = {}
    if days:
        ph = ",".join(["?"] * len(days))
        exist = conn.execute("SELECT p.monteur_id AS mid, p.date AS date, p.order_id AS oid, o2.city AS city "
                             "FROM planning p JOIN orders o2 ON o2.id=p.order_id WHERE p.date IN (%s)" % ph,
                             days).fetchall()
        exist_mw = _mw_map([r["oid"] for r in exist])
        for r in exist:
            mw = exist_mw.get(r["oid"], (30, 0))
            routes_state.setdefault((r["mid"], r["date"]), []).append(
                {"coord": _coord(r["city"]), "prov": PROVINCE.get(r["city"]),
                 "montage": mw[0] or 30, "weight": mw[1] or 0})

    cand_mw = _mw_map([o["id"] for o in orders])
    busmap = {m["id"]: m["bus_id"] for m in monteurs}
    busweights = {b["id"]: ((b["max_weight"] or 0) - (b["empty_weight"] or 0))
                  for b in conn.execute("SELECT id,max_weight,empty_weight FROM busses").fetchall()}
    home_prov = {m["id"]: PROVINCE.get(_home_city(m)) for m in monteurs}
    planned = notfit = 0
    for o in orders:
        oprov = PROVINCE.get(o["city"])
        mw = cand_mw.get(o["id"], (0, 0))
        omont = mw[0] or (o["montage_min"] or 30)
        oweight = mw[1] or 0
        ocoord = _coord(o["city"])
        best, best_score = None, -1e9
        for di, diso in enumerate(days):
            for m in monteurs:
                if is_free(m["id"], diso):
                    continue
                cur = routes_state.get((m["id"], diso), [])
                rt = _route_min([s["coord"] for s in cur] + [ocoord],
                                sum(s["montage"] for s in cur) + omont)
                if rt > WORKDAY_MIN:
                    continue                       # past niet binnen de werkdag van 7 uur
                cap = busweights.get(busmap.get(m["id"]) or 0, 0)
                if cap > 0 and (sum(s["weight"] for s in cur) + oweight) > cap + LOAD_MARGIN_KG:
                    continue                       # past niet qua laadgewicht (incl. marge)
                provs_after = set(p for p in ([s["prov"] for s in cur] + [oprov]) if p)
                score = 0.0
                if oprov and oprov in set(s["prov"] for s in cur):
                    score += 120                   # clustert bij dezelfde provincie die dag
                if oprov and home_prov.get(m["id"]) == oprov:
                    score += 30                    # monteur woont in die regio
                if len(provs_after) >= 3:
                    score -= 500                   # vermijd 3+ provincies (zoals de waarschuwing)
                score -= di * 10                   # liever eerder in de week
                score -= rt * 0.03                 # lichtere dag licht voorkeur
                if score > best_score:
                    best_score, best = score, (m["id"], diso, len(cur))
        if best:
            mid, diso, seq = best
            conn.execute("""INSERT INTO planning(order_id,monteur_id,bus_id,date,sequence,status,mailed)
                            VALUES(?,?,?,?,?,'gepland',0)""", (o["id"], mid, busmap.get(mid), diso, seq))
            conn.execute("UPDATE orders SET status='gepland' WHERE id=?", (o["id"],))
            routes_state.setdefault((mid, diso), []).append({"coord": ocoord, "prov": oprov, "montage": omont, "weight": oweight})
            planned += 1
        else:
            notfit += 1               # paste nergens binnen de werkdag én het laadgewicht
    conn.commit()
    conn.close()
    return jsonify(ok=True, planned=planned, notfit=notfit)


@bp.route("/api/manual-order", methods=["POST"])
def api_manual_order():
    """Handmatig een adres/levering toevoegen aan de planning. Bij heropenen worden
    alle tijden (GT/AT), ETA, regio en km automatisch herberekend."""
    if not has_perm("edit_planning"):
        return jsonify(ok=False, error="Geen rechten"), 403
    f = request.form
    name = (f.get("client") or "Handmatige klant").strip()
    address = (f.get("address") or "").strip()
    city = (f.get("city") or "").strip()
    postal = (f.get("postal") or "").strip()
    full = ", ".join([p for p in [address, (postal + " " + city).strip()] if p])
    mid = f.get("monteur_id") or None
    day = f.get("date") or _today_iso()
    montage = _int(f.get("montage_min"), 30)
    try:
        amount = float(f.get("amount") or 0)
    except (ValueError, TypeError):
        amount = 0.0
    service = f.get("service_type") or "montage"
    items = (f.get("items") or "").strip()
    email = (f.get("email") or "").strip()
    conn = db()
    cl = conn.execute("SELECT id FROM clients WHERE name=?", (name,)).fetchone()
    if cl:
        cid = cl["id"]
    else:
        conn.execute("INSERT INTO clients(name,email,address,postal,city,created_at) VALUES(?,?,?,?,?,?)",
                     (name, email, address, postal, city, _today_iso()))
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    onum = "H" + datetime.now().strftime("%y%m%d%H%M%S")[-6:]
    conn.execute("""INSERT INTO orders(order_number,client_id,source,is_draft,status,delivery_address,city,postal,
                    invoice_address,email,desired_date,amount,montage_min,service_type,track_token,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (onum, cid, "manual", 0, "gepland", full, city, postal, full, email, day, amount, montage, service, _new_track_token(), _today_iso()))
    oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if items:
        conn.execute("INSERT INTO order_items(order_id,name,qty) VALUES(?,?,1)", (oid, items))
    # Pakbon-PDF (bij ophalen): opslaan en koppelen aan de order voor de monteur.
    pf = request.files.get("pakbon")
    if pf and pf.filename and pf.filename.lower().endswith(".pdf"):
        fn = "pakbon_%d_%s" % (oid, secure_filename(pf.filename))
        try:
            pf.save(os.path.join(UPLOAD_DIR, fn))
            conn.execute("UPDATE orders SET pakbon=? WHERE id=?", (fn, oid))
        except Exception:
            pass
    if mid:
        seq = conn.execute("SELECT COUNT(*) FROM planning WHERE monteur_id=? AND date=?", (mid, day)).fetchone()[0]
        mb = conn.execute("SELECT bus_id FROM monteurs WHERE id=?", (mid,)).fetchone()
        conn.execute("""INSERT INTO planning(order_id,monteur_id,bus_id,date,sequence,status,mailed)
                        VALUES(?,?,?,?,?,'gepland',0)""", (oid, mid, (mb["bus_id"] if mb else None), day, seq))
    else:
        conn.execute("UPDATE orders SET status='in_te_plannen' WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/send-confirmations", methods=["POST"])
def api_send_confirmations():
    """Verstuur in één klik de bevestigingsmails voor nieuw geplande orders."""
    if not has_perm("inform_clients"):
        return jsonify(ok=False, error="Geen rechten"), 403
    if not _mail_live():
        # Testmodus: niets versturen, niets markeren — alleen melden hoeveel het zouden zijn.
        conn = db()
        n = conn.execute("SELECT COUNT(*) FROM planning WHERE mailed=0 AND confirmed=0 AND status!='afgerond'").fetchone()[0]
        conn.close()
        return jsonify(ok=True, sent=0, would=n, mail_live=False)
    conn = db()
    rows = conn.execute("""SELECT p.id AS pid, p.date, p.slot_start, p.slot_end,
                           o.order_number, o.client_id, o.email AS oemail, c.name AS client, c.email AS cemail
                           FROM planning p JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
                           WHERE p.mailed=0 AND p.confirmed=0 AND p.status!='afgerond'""").fetchall()
    sent = 0
    for r in rows:
        subject, body, html = _planning_confirmation_mail(r["client"], r["date"], r["slot_start"],
                                                          r["slot_end"], r["order_number"])
        _send_mail((r["oemail"] or r["cemail"]), subject, body, html)
        conn.execute("""INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment)
                        VALUES(?,?,?,?,?,0)""",
                     (r["client_id"], "out", subject, body, datetime.now().isoformat(timespec="minutes")))
        conn.execute("UPDATE planning SET mailed=1 WHERE id=?", (r["pid"],))
        sent += 1
    conn.commit()
    conn.close()
    return jsonify(ok=True, sent=sent, mail_live=True)


@bp.route("/pakbon/<int:oid>")
def pakbon(oid):
    if not current_user():
        abort(403)
    conn = db()
    o = conn.execute("SELECT pakbon FROM orders WHERE id=?", (oid,)).fetchone()
    conn.close()
    if not o or not o["pakbon"]:
        abort(404)
    return send_from_directory(UPLOAD_DIR, o["pakbon"])


@bp.route("/api/planning-version")
def api_planning_version():
    """Versie-stempel van de dagplanning; de planning-pagina pollt dit voor live updates."""
    if not current_user():
        return jsonify(v=""), 403
    day = request.args.get("day", _today_iso())
    conn = db()
    r = conn.execute("""SELECT COUNT(*) c, COALESCE(MAX(id),0) m, COALESCE(SUM(confirmed),0) cf,
                        COALESCE(SUM(CASE WHEN status='afgerond' THEN 1 ELSE 0 END),0) af
                        FROM planning WHERE date=?""", (day,)).fetchone()
    conn.close()
    return jsonify(v="%d-%d-%d-%d" % (r["c"], r["m"], r["cf"], r["af"]))


@bp.route("/api/order-contact/<int:oid>")
def api_order_contact(oid):
    """Adres + e-mailgegevens van een order (voor het adres-popupje en de mailknop)."""
    if not current_user():
        return jsonify(ok=False), 403
    conn = db()
    o = conn.execute("""SELECT o.order_number, o.delivery_address, o.email, o.phone, c.name AS client
                        FROM orders o LEFT JOIN clients c ON c.id=o.client_id WHERE o.id=?""", (oid,)).fetchone()
    conn.close()
    if not o:
        return jsonify(ok=False), 404
    return jsonify(ok=True, **dict(o))


@bp.route("/track/<token>")
def track(token):
    """Publieke volgpagina voor de klant (geen login). Toont status + tijdvak + live ETA.
    Werkt op een niet-raadbaar token i.p.v. het ordernummer (voorkomt enumeratie)."""
    conn = db()
    _purge_old_customer_notes(conn)
    o = conn.execute("""SELECT o.id, o.order_number, o.city, o.status AS ostatus, o.customer_note, c.name AS client
                        FROM orders o LEFT JOIN clients c ON c.id=o.client_id WHERE o.track_token=?""",
                     (token,)).fetchone()
    if not o:
        conn.close()
        return render_template("planning/track.html", found=False, onum=None)
    p = conn.execute("""SELECT p.*, m.name AS monteur, m.id AS mid FROM planning p
                        LEFT JOIN monteurs m ON m.id=p.monteur_id WHERE p.order_id=?""", (o["id"],)).fetchone()
    status = (p["status"] if p else None) or o["ostatus"] or "in_te_plannen"
    tijdvak = the_date = eta = monteur = None
    m_lat = m_lng = None
    dcoord = CITY_COORDS.get(o["city"]) or BREDA
    if p:
        the_date = p["date"]
        monteur = p["monteur"]
        if p["slot_start"]:
            tijdvak = (p["slot_start"] or "") + " – " + (p["slot_end"] or "")
        if status == "onderweg" and p["mid"]:
            try:
                live = _live_loc(conn, p["mid"])
                if live:
                    m_lat, m_lng = live[0], live[1]
                    stops = conn.execute("""SELECT p.order_id, p.slot_start, p.status, o.city, o.montage_min
                                            FROM planning p JOIN orders o ON o.id=p.order_id
                                            WHERE p.monteur_id=? AND p.date=? ORDER BY p.sequence""",
                                         (p["mid"], p["date"])).fetchall()
                    m = conn.execute("SELECT * FROM monteurs WHERE id=?", (p["mid"],)).fetchone()
                    arrivals, _ = compute_arrivals(stops, m, live, True)
                    for st, a in zip(stops, arrivals):
                        if st["order_id"] == o["id"]:
                            eta = a.get("at")
                            break
            except Exception:
                eta = None
    conn.close()
    return render_template("planning/track.html", found=True, onum=o["order_number"], token=token,
                           client=o["client"], status=status,
                           tijdvak=tijdvak, the_date=_nl_date(the_date) if the_date else None,
                           eta=eta, monteur=monteur, note=o["customer_note"],
                           can_note=(status in ("gepland", "onderweg")), saved=request.args.get("saved"),
                           m_lat=m_lat, m_lng=m_lng, d_lat=dcoord[0], d_lng=dcoord[1])


@bp.route("/track/<token>/note", methods=["POST"])
def track_note(token):
    """Klant geeft (max 100 tekens) iets door aan de chauffeur — publiek, via het niet-raadbare token."""
    note = (request.form.get("note") or "").strip()[:100]
    conn = db()
    conn.execute("UPDATE orders SET customer_note=? WHERE track_token=?", (note, token))
    conn.commit()
    conn.close()
    return redirect(url_for("planning.track", token=token, saved=1))


@bp.route("/api/correspondence/<int:oid>")
def api_correspondence(oid):
    """Alle e-mailcorrespondentie van de klant achter deze order (voor de snelweergave)."""
    if not current_user():
        return jsonify(ok=False), 403
    conn = db()
    o = conn.execute("SELECT client_id FROM orders WHERE id=?", (oid,)).fetchone()
    items = []
    if o:
        items = [dict(r) for r in conn.execute(
            """SELECT direction, subject, body, ts FROM email_log
               WHERE client_id=? ORDER BY ts DESC, id DESC LIMIT 50""", (o["client_id"],)).fetchall()]
    conn.close()
    return jsonify(ok=True, items=items)


@bp.route("/email-templates", methods=["GET", "POST"])
def email_templates():
    """Bewerk de teksten van de klantmails (kop + body) met live voorbeeld in de huisstijl."""
    guard = login_required("manage_settings")
    if guard:
        return guard
    keys = list(MAIL_TEXT_DEFAULTS.keys())
    if request.method == "POST":
        conn = db()
        for k in keys:
            conn.execute("INSERT INTO settings(skey,value) VALUES(?,?) "
                         "ON CONFLICT(skey) DO UPDATE SET value=excluded.value",
                         (k, (request.form.get(k) or "").strip()))
        conn.commit()
        conn.close()
        flash("E-mailteksten opgeslagen.")
        return redirect(url_for("planning.email_templates"))
    cur = {k: _mailtxt(k) for k in keys}

    def prev(hk, bk, info, button=None, note=None):
        return _brand_email(cur[hk], _paras("Beste Voorbeeldklant,", cur[bk]), info=info, button=button, note=note)

    previews = {
        "confirm": prev("mailtxt_confirm_h", "mailtxt_confirm_b",
                        [("Bezorgdatum", "vrijdag 4 juli"), ("Verwachte tijd", "09:00 – 12:00"), ("Ordernummer", "#36399")],
                        note="Op de dag zelf ontvangt u een mail met een live volglink en de verwachte aankomsttijd van de monteur."),
        "today": prev("mailtxt_today_h", "mailtxt_today_b",
                      [("Bezorgdatum", "vrijdag 4 juli"), ("Tijdvak", "08:30–10:30"), ("Ordernummer", "#36399")],
                      button=("Volg uw levering & bericht doorgeven", "#")),
        "near": prev("mailtxt_near_h", "mailtxt_near_b",
                     [("Monteur", "Tom"), ("Verwachte aankomst", "rond 09:55"), ("Ordernummer", "#36399")],
                     button=("Volg live op de kaart", "#")),
        "delay": prev("mailtxt_delay_h", "mailtxt_delay_b",
                      [("Nieuwe verwachte tijd", "rond 10:40"), ("Ordernummer", "#36399")],
                      button=("Volg uw levering", "#")),
    }
    blocks = [
        ("confirm", "Bevestiging — automatisch ná het inplannen"),
        ("today", "Wij komen vandaag langs"),
        ("near", "Onze monteur is er bijna"),
        ("delay", "Update over uw levertijd"),
    ]
    return render_template("planning/email_templates.html", cur=cur, previews=previews, blocks=blocks)


@bp.route("/api/mail", methods=["POST"])
def api_mail():
    """Verstuur (via de centrale Gmail-mailbox) en bewaar in het klantdossier."""
    if not has_perm("inform_clients") and not has_perm("edit_clients"):
        return jsonify(ok=False, error="Geen rechten"), 403
    data = request.get_json(force=True)
    oid = int(data["order_id"])
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    conn = db()
    o = conn.execute("""SELECT o.client_id, o.email AS oemail, c.email AS cemail
                        FROM orders o LEFT JOIN clients c ON c.id=o.client_id WHERE o.id=?""", (oid,)).fetchone()
    sent_ok = False
    if o:
        html = _brand_email(subject or "Bericht van Office-Interior", [body])
        sent_ok = _send_mail((o["oemail"] or o["cemail"]), subject, body, html)
        conn.execute("""INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment)
                        VALUES(?,?,?,?,?,0)""",
                     (o["client_id"], "out", subject, body, datetime.now().isoformat(timespec="minutes")))
        conn.commit()
    conn.close()
    return jsonify(ok=True, mail_live=_mail_live(),
                   message=("Verzonden vanuit planning@office-interior.com en bewaard in het klantdossier."
                            if sent_ok else
                            "Opgeslagen in het klantdossier. Er is nog NIETS verstuurd — zet 'E-mails écht versturen' aan onder Koppelingen → Klantmail om live te gaan."))


# --------------------------------------------------------------------------- #
#  Orders + belangrijke orders
# --------------------------------------------------------------------------- #
@bp.route("/orders")
def orders():
    guard = login_required("view_orders")
    if guard:
        return guard
    status = request.args.get("status", "")
    conn = db()
    q = """SELECT o.*, c.name AS client, (SELECT COUNT(*) FROM order_items WHERE order_id=o.id) AS n_items
           FROM orders o LEFT JOIN clients c ON c.id=o.client_id"""
    args = ()
    if status:
        q += " WHERE o.status=?"
        args = (status,)
    sort = request.args.get("sort", "nieuw")
    q += " ORDER BY o.id ASC" if sort == "oud" else " ORDER BY o.id DESC"
    rows = conn.execute(q, args).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) AS n FROM orders GROUP BY status")}
    conn.close()
    u = current_user()
    return render_template("planning/orders.html", orders=rows, counts=counts, status=status, sort=sort,
                           is_admin=(u["role"] == "beheerder" if u else False))


@bp.route("/orders/belangrijk")
def important_orders():
    guard = login_required("view_orders")
    if guard:
        return guard
    today = _today_iso()
    sort = request.args.get("sort", "nieuw")
    order_by = "o.id ASC" if sort == "oud" else "o.id DESC"
    conn = db()
    rows = conn.execute("""SELECT o.*, c.name AS client,
                           (SELECT COUNT(*) FROM order_items WHERE order_id=o.id) AS n_items
                           FROM orders o LEFT JOIN clients c ON c.id=o.client_id
                           WHERE o.amount>=? AND o.is_draft=0 AND o.desired_date>=?
                           ORDER BY """ + order_by, (IMPORTANT_THRESHOLD, today)).fetchall()
    items = _items_by_order(conn, [o["id"] for o in rows])
    conn.close()
    return render_template("planning/important_orders.html", orders=rows, items=items,
                           threshold=IMPORTANT_THRESHOLD, sort=sort)


@bp.route("/orders/<int:oid>")
def order_detail(oid):
    guard = login_required("view_orders")
    if guard:
        return guard
    conn = db()
    o = conn.execute("""SELECT o.*, c.name AS client FROM orders o
                        LEFT JOIN clients c ON c.id=o.client_id WHERE o.id=?""", (oid,)).fetchone()
    if not o:
        conn.close(); abort(404)
    items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()
    plan = conn.execute("""SELECT p.*, m.name AS monteur FROM planning p
                           LEFT JOIN monteurs m ON m.id=p.monteur_id WHERE p.order_id=?""", (oid,)).fetchone()
    prods = _load_products(conn)
    n_open = sum(1 for it in items if it["montage_custom"] is None and not _match_product(it["name"], prods))
    workload = _order_montage([{"name": it["name"], "qty": it["qty"], "montage_custom": it["montage_custom"]} for it in items],
                              prods, fallback=(o["montage_min"] or 0), service_type=o["service_type"])
    conn.close()
    return render_template("planning/order_detail.html", o=o, items=items, plan=plan,
                           needs_maatwerk=(n_open > 0 and _maatwerk_alerts_on()), n_open=n_open, workload=workload)


# --------------------------------------------------------------------------- #
#  Klanten
# --------------------------------------------------------------------------- #
@bp.route("/clients")
def clients():
    guard = login_required("view_orders")
    if guard:
        return guard
    conn = db()
    rows = conn.execute("""SELECT c.*, (SELECT COUNT(*) FROM orders WHERE client_id=c.id) AS n_orders
                           FROM clients c ORDER BY c.name""").fetchall()
    conn.close()
    return render_template("planning/clients.html", clients=rows)


@bp.route("/clients/<int:cid>")
def client_detail(cid):
    guard = login_required("view_orders")
    if guard:
        return guard
    conn = db()
    cl = conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not cl:
        conn.close(); abort(404)
    orders = conn.execute("SELECT * FROM orders WHERE client_id=? ORDER BY id DESC", (cid,)).fetchall()
    emails = conn.execute("SELECT * FROM email_log WHERE client_id=? ORDER BY ts DESC, id DESC", (cid,)).fetchall()
    conn.close()
    return render_template("planning/client_detail.html", c=cl, orders=orders, emails=emails,
                           gmail_ready=(integ_status("gmail") == "verbonden"))


# --------------------------------------------------------------------------- #
#  Teamchat (onderling chatten over lopende orders)
# --------------------------------------------------------------------------- #
@bp.route("/chat")
def chat():
    guard = login_required("view_orders")
    if guard:
        return guard
    conn = db()
    msgs = conn.execute("""SELECT m.id, m.text, m.order_number, m.ts, m.user_id, u.name AS user
                           FROM chat_messages m LEFT JOIN users u ON u.id=m.user_id
                           ORDER BY m.id DESC LIMIT 80""").fetchall()
    conn.close()
    msgs = list(reversed(msgs))
    last_id = msgs[-1]["id"] if msgs else 0
    return render_template("planning/chat.html", messages=msgs, last_id=last_id,
                           me=current_user()["id"])


@bp.route("/api/chat", methods=["GET", "POST"])
def api_chat():
    u = current_user()
    if not u:
        return jsonify(ok=False), 403
    if request.method == "POST":
        data = request.get_json(force=True)
        text = (data.get("text") or "").strip()
        onum = (data.get("order_number") or "").strip().lstrip("#")
        if text:
            conn = db()
            conn.execute("INSERT INTO chat_messages(user_id,text,order_number,ts) VALUES(?,?,?,?)",
                         (u["id"], text, onum or None, datetime.now().isoformat(timespec="minutes")))
            conn.commit()
            conn.close()
        return jsonify(ok=True)
    after = int(request.args.get("after", 0))
    conn = db()
    rows = conn.execute("""SELECT m.id, m.text, m.order_number, m.ts, m.user_id, u.name AS user
                           FROM chat_messages m LEFT JOIN users u ON u.id=m.user_id
                           WHERE m.id>? ORDER BY m.id LIMIT 200""", (after,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# --------------------------------------------------------------------------- #
#  Live gebruikers
# --------------------------------------------------------------------------- #
@bp.route("/api/online")
def api_online():
    if not current_user():
        return jsonify([]), 403
    return jsonify(online_users())


# --------------------------------------------------------------------------- #
#  Monteurs (incl. bewerken: snelheid, thuisadres, bus)
# --------------------------------------------------------------------------- #
@bp.route("/monteurs")
def monteurs():
    guard = login_required("view_personnel")
    if guard:
        return guard
    conn = db()
    rows = conn.execute("""SELECT m.*, b.name AS bus FROM monteurs m
                           LEFT JOIN busses b ON b.id=m.bus_id ORDER BY m.name""").fetchall()
    busses = conn.execute("SELECT * FROM busses WHERE active=1 ORDER BY name").fetchall()
    conn.close()
    return render_template("planning/monteurs.html", monteurs=rows, busses=busses,
                           can_edit=has_perm("manage_users"))


@bp.route("/monteurs/<int:mid>/edit", methods=["POST"])
def monteur_edit(mid):
    if not has_perm("manage_users"):
        abort(403)
    f = request.form
    conn = db()
    conn.execute("""UPDATE monteurs SET name=?, phone=?, email=?, speed=?, bus_id=?, home_address=?, active=? WHERE id=?""",
                 (f.get("name"), f.get("phone"), f.get("email"), _int(f.get("speed"), 3),
                  (f.get("bus_id") or None), f.get("home_address"),
                  1 if f.get("active") else 0, mid))
    conn.commit()
    conn.close()
    flash("Monteur bijgewerkt.")
    return redirect(url_for("planning.monteurs"))


# --------------------------------------------------------------------------- #
#  Urenregister — uren die monteurs vanuit de app doorgeven (inhoud alleen met recht)
# --------------------------------------------------------------------------- #
@bp.route("/urenregister")
def urenregister():
    guard = login_required()          # inloggen volstaat om de tab te openen
    if guard:
        return guard
    can_view = has_perm("view_hours")  # inhoud alleen voor wie het recht heeft (standaard beheerder)
    entries, monteurs_list, range_total, range_ot, warn_count = [], [], 0, 0, 0
    sel_monteur = request.args.get("monteur") or ""
    van = request.args.get("van") or ""
    tot = request.args.get("tot") or ""
    # Periode: standaard de huidige week; met van/tot een vrij datumbereik.
    custom, start, end = False, None, None
    try:
        if van and tot:
            sd = datetime.strptime(van, "%Y-%m-%d").date()
            ed = datetime.strptime(tot, "%Y-%m-%d").date()
            if ed < sd:
                sd, ed = ed, sd
            start, end, custom = sd, ed, True
    except Exception:
        custom = False
    if not custom:
        base = request.args.get("d") or _today_iso()
        try:
            bd = datetime.strptime(base, "%Y-%m-%d").date()
        except Exception:
            bd = datetime.now().date()
        start = bd - timedelta(days=bd.weekday())
        end = start + timedelta(days=6)
    prev_week = (start - timedelta(days=7)).isoformat()
    next_week = (start + timedelta(days=7)).isoformat()
    if custom:
        range_label = "%d %s – %d %s %d" % (start.day, _NL_MONTHS[start.month][:3],
                                            end.day, _NL_MONTHS[end.month][:3], end.year)
    else:
        range_label = "Week %d · %d %s – %d %s" % (start.isocalendar()[1], start.day,
                                                   _NL_MONTHS[start.month][:3], end.day, _NL_MONTHS[end.month][:3])
    van_val, tot_val = (van or start.isoformat()), (tot or end.isoformat())
    if can_view:
        conn = db()
        monteurs_list = conn.execute("SELECT id,name FROM monteurs ORDER BY name").fetchall()
        q = ("""SELECT w.*, COALESCE(m.name, w.user_name) AS monteur_name FROM work_hours w
                LEFT JOIN monteurs m ON m.id=w.monteur_id
                WHERE w.work_date>=? AND w.work_date<=?""")
        params = [start.isoformat(), end.isoformat()]
        if sel_monteur:
            q += " AND w.monteur_id=?"; params.append(int(sel_monteur))
        q += " ORDER BY w.work_date, monteur_name"
        rows = conn.execute(q, tuple(params)).fetchall()
        ws, we = start.isoformat(), end.isoformat()
        # GPS-thuiskomst per monteur/dag (leidend) + route-afsluiting (terugval)
        gps = {(r["monteur_id"], r["date"]): r["home_since"] for r in
               conn.execute("SELECT monteur_id,date,home_since FROM monteur_day_gps WHERE date>=? AND date<=?",
                            (ws, we)).fetchall() if r["home_since"]}
        try:
            closed = {(r["monteur_id"], r["date"]): r["ts"] for r in
                      conn.execute("SELECT monteur_id,date,ts FROM route_closed WHERE date>=? AND date<=?",
                                   (ws, we)).fetchall()}
        except Exception:
            closed = {}
        conn.close()

        def _mn(hm):
            try:
                h, mm = hm.split(":")[:2]
                return int(h) * 60 + int(mm)
            except Exception:
                return None

        for r in rows:
            key = (r["monteur_id"], r["work_date"])
            gt, ct = gps.get(key), closed.get(key)
            ref = gt or ct
            warn, warn_text, warn_over = False, "", 0
            if ref and r["end_time"]:
                rt = ref.split("T")[1][:5] if "T" in ref else ref[:5]
                emin, rmin = _mn(r["end_time"]), _mn(rt)
                if emin is not None and rmin is not None and emin - rmin >= 30:
                    warn, warn_over = True, emin - rmin
                    src = ("GPS: thuis om %s" % rt) if gt else ("Route afgesloten om %s" % rt)
                    warn_text = "%s, maar uren ingevuld tot %s (+%d min meer dan gewerkt)." % (src, r["end_time"], warn_over)
                    warn_count += 1
            entries.append({"date": r["work_date"], "monteur": r["monteur_name"] or "—",
                            "start": r["start_time"], "end": r["end_time"],
                            "worked_min": r["worked_min"] or 0, "overtime_min": r["overtime_min"] or 0,
                            "note": r["note"] or "", "warn": warn, "warn_text": warn_text, "warn_over": warn_over})
        range_total = sum(e["worked_min"] for e in entries)
        range_ot = sum(e["overtime_min"] for e in entries)
    u = current_user()
    return render_template("planning/urenregister.html", can_view=can_view, entries=entries,
                           monteurs=monteurs_list, sel_monteur=sel_monteur, range_label=range_label,
                           prev_week=prev_week, next_week=next_week, range_total=range_total,
                           range_ot=range_ot, warn_count=warn_count, nl_date=_nl_date, custom=custom,
                           van=van_val, tot=tot_val, is_admin=bool(u and u["role"] == "beheerder"))


@bp.route("/urenregister/demo", methods=["POST"])
def urenregister_demo():
    """Vul het urenregister met testdata (beheerder) — incl. één verdachte regel."""
    u = current_user()
    if not u or u["role"] != "beheerder":
        abort(403)
    conn = db()
    monteurs = conn.execute("SELECT id,name FROM monteurs WHERE active=1 ORDER BY id LIMIT 4").fetchall()
    if not monteurs:
        conn.close()
        flash("Geen actieve monteurs om testdata voor te maken.")
        return redirect(url_for("planning.urenregister"))
    monday = datetime.now().date() - timedelta(days=datetime.now().date().weekday())

    def ins(mid, name, offset, start, end):
        d = (monday + timedelta(days=offset)).isoformat()
        sm = int(start[:2]) * 60 + int(start[3:])
        em = int(end[:2]) * 60 + int(end[3:])
        worked = max(0, em - sm)
        ot = max(0, worked - 480)
        conn.execute("""INSERT INTO work_hours(monteur_id,user_id,user_email,user_name,work_date,start_time,
                        end_time,worked_min,overtime_min,note,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(monteur_id,work_date) DO UPDATE SET user_id=0,start_time=excluded.start_time,
                        end_time=excluded.end_time,worked_min=excluded.worked_min,overtime_min=excluded.overtime_min,
                        submitted_at=excluded.submitted_at""",
                     (mid, 0, "demo", name, d, start, end, worked, ot, "", datetime.now().isoformat(timespec="minutes")))

    m0 = monteurs[0]
    ins(m0["id"], m0["name"], 0, "08:00", "16:30")
    ins(m0["id"], m0["name"], 1, "07:00", "17:00")
    ins(m0["id"], m0["name"], 2, "08:00", "17:30")   # verdacht: GPS thuis 17:00
    d_wed = (monday + timedelta(days=2)).isoformat()
    conn.execute("""INSERT INTO monteur_day_gps(monteur_id,date,home_since) VALUES(?,?,?)
                    ON CONFLICT(monteur_id,date) DO UPDATE SET home_since=excluded.home_since""",
                 (m0["id"], d_wed, d_wed + "T17:00"))
    if len(monteurs) > 1:
        m1 = monteurs[1]
        ins(m1["id"], m1["name"], 0, "07:30", "18:00")
        ins(m1["id"], m1["name"], 1, "08:00", "16:00")
    conn.commit()
    conn.close()
    flash("Testdata toegevoegd — inclusief één verdachte regel (GPS thuis 17:00, uren tot 17:30).")
    return redirect(url_for("planning.urenregister", d=monday.isoformat()))


@bp.route("/urenregister/demo-clear", methods=["POST"])
def urenregister_demo_clear():
    u = current_user()
    if not u or u["role"] != "beheerder":
        abort(403)
    conn = db()
    conn.execute("DELETE FROM work_hours WHERE user_id=0")
    m0 = conn.execute("SELECT id FROM monteurs WHERE active=1 ORDER BY id LIMIT 1").fetchone()
    if m0:
        monday = datetime.now().date() - timedelta(days=datetime.now().date().weekday())
        friday = (monday + timedelta(days=4)).isoformat()
        conn.execute("DELETE FROM monteur_day_gps WHERE monteur_id=? AND date>=? AND date<=?",
                     (m0["id"], monday.isoformat(), friday))
    conn.commit()
    conn.close()
    flash("Testdata gewist.")
    return redirect(url_for("planning.urenregister"))


# --------------------------------------------------------------------------- #
#  Bussen (bewerkbaar)
# --------------------------------------------------------------------------- #
@bp.route("/busses")
def busses():
    guard = login_required("view_personnel")
    if guard:
        return guard
    conn = db()
    rows = conn.execute("SELECT * FROM busses ORDER BY name").fetchall()
    notes_by_bus = {}
    for n in conn.execute("""SELECT id, bus_id, note, important, image_mime, image_name, author, created_at
                             FROM bus_notes ORDER BY important DESC, id DESC""").fetchall():
        notes_by_bus.setdefault(n["bus_id"], []).append(n)
    conn.close()
    return render_template("planning/busses.html", busses=rows, today=_today_iso(),
                           can_edit=has_perm("manage_users"), notes_by_bus=notes_by_bus,
                           margin_kg=LOAD_MARGIN_KG)


@bp.route("/busses/<int:bid>/note", methods=["POST"])
def bus_note_add(bid):
    if not has_perm("view_personnel"):
        abort(403)
    note = (request.form.get("note") or "").strip()
    important = 1 if request.form.get("important") else 0
    f = request.files.get("photo")
    img_data = img_mime = img_name = None
    if f and f.filename:
        data = f.read()
        if len(data) > MAX_DOC_BYTES:
            flash("Foto is te groot (max 15 MB).")
            return redirect(url_for("planning.busses"))
        img_data = data
        img_mime = f.mimetype or "image/jpeg"
        img_name = secure_filename(f.filename) or "foto"
    if not note and img_data is None:
        flash("Voeg een opmerking of foto toe.")
        return redirect(url_for("planning.busses"))
    u = current_user()
    conn = db()
    conn.execute("""INSERT INTO bus_notes(bus_id,note,important,image_data,image_mime,image_name,author,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                 (bid, note, important, img_data, img_mime, img_name,
                  (u["name"] if u else ""), datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()
    flash("Opmerking toegevoegd.")
    return redirect(url_for("planning.busses"))


@bp.route("/busses/note/<int:nid>/foto")
def bus_note_photo(nid):
    guard = login_required("view_personnel")
    if guard:
        return guard
    conn = db()
    row = conn.execute("SELECT image_data, image_mime, image_name FROM bus_notes WHERE id=?", (nid,)).fetchone()
    conn.close()
    if not row or row["image_data"] is None:
        abort(404)
    fname = (row["image_name"] or "foto").replace('"', "")
    return Response(bytes(row["image_data"]), mimetype=(row["image_mime"] or "image/jpeg"),
                    headers={"Content-Disposition": 'inline; filename="%s"' % fname})


@bp.route("/busses/note/<int:nid>/verwijderen", methods=["POST"])
def bus_note_delete(nid):
    if not has_perm("view_personnel"):
        abort(403)
    conn = db()
    conn.execute("DELETE FROM bus_notes WHERE id=?", (nid,))
    conn.commit()
    conn.close()
    flash("Opmerking verwijderd.")
    return redirect(url_for("planning.busses"))


@bp.route("/busses/<int:bid>/edit", methods=["POST"])
def bus_edit(bid):
    if not has_perm("manage_users"):
        abort(403)
    f = request.form
    conn = db()
    def _f(v):
        try:
            return float(v or 0)
        except (ValueError, TypeError):
            return 0.0
    conn.execute("""UPDATE busses SET name=?, plate=?, driver=?, max_volume=?, max_weight=?, empty_weight=?,
                    max_stops=?, apk_date=?, maintenance=?, active=? WHERE id=?""",
                 (f.get("name"), f.get("plate"), f.get("driver"),
                  _f(f.get("max_volume")), _f(f.get("max_weight")), _f(f.get("empty_weight")),
                  _int(f.get("max_stops"), 0), f.get("apk_date"), f.get("maintenance"),
                  1 if f.get("active") else 0, bid))
    conn.commit()
    conn.close()
    flash("Bus bijgewerkt.")
    return redirect(url_for("planning.busses"))


# --------------------------------------------------------------------------- #
#  Bus-issues (door monteurs gemeld vanuit de app)
# --------------------------------------------------------------------------- #
@bp.route("/bus-issues")
def bus_issues():
    guard = login_required("view_personnel")
    if guard:
        return guard
    conn = db()
    rows = conn.execute("""SELECT * FROM bus_issues
                           ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, created_at DESC, id DESC""").fetchall()
    conn.close()
    return render_template("planning/bus_issues.html", issues=rows)


@bp.route("/bus-issues/resolve/<int:iid>", methods=["POST"])
def bus_issue_resolve(iid):
    guard = login_required("view_personnel")
    if guard:
        return guard
    u = current_user()
    conn = db()
    conn.execute("UPDATE bus_issues SET status='opgelost', resolved_by=?, resolved_at=? WHERE id=?",
                 (u["name"], datetime.now().isoformat(timespec="minutes"), iid))
    conn.commit()
    conn.close()
    flash("Bus-issue gemarkeerd als opgelost.")
    return redirect(url_for("planning.bus_issues"))


# --------------------------------------------------------------------------- #
#  Magazijn: Voormonteren + Picklijst
# --------------------------------------------------------------------------- #
VOORMONTAGE_CATS = ["Bureaustoelen", "Bureaus", "Scheidingswanden", "Overige"]


def _voormontage_cat(name):
    """Deel een productnaam in: Bureaustoelen, Bureaus, Scheidingswanden of Overige."""
    n = (name or "").lower()
    if "stoel" in n:
        return "Bureaustoelen"
    if "scheidingswand" in n or "akoest" in n or "paneel" in n or ("wand" in n and "kast" not in n):
        return "Scheidingswanden"
    if "bureau" in n:
        return "Bureaus"
    return "Overige"


@bp.route("/voormonteren")
def voormonteren():
    guard = login_required("view_preassembly")
    if guard:
        return guard
    sel = request.args.get("cat") or "alle"
    if sel not in VOORMONTAGE_CATS:
        sel = "alle"
    conn = db()
    today = _today_iso()
    rows = conn.execute("""
        SELECT p.date AS date, oi.name AS name, oi.qty AS qty
        FROM planning p
        JOIN orders o ON o.id = p.order_id
        JOIN order_items oi ON oi.order_id = o.id
        WHERE p.date >= ? AND p.status != 'afgerond'
        ORDER BY p.date, oi.name
    """, (today,)).fetchall()
    conn.close()

    days = {}
    for r in rows:
        nm = r["name"] or ""
        qty = int(r["qty"] or 1)
        d = r["date"]
        day = days.setdefault(d, {"date": d, "label": _nl_date(d),
                                  "cats": {c: {} for c in VOORMONTAGE_CATS}})
        cat = _voormontage_cat(nm)
        day["cats"][cat][nm] = day["cats"][cat].get(nm, 0) + qty

    day_list = []
    for d in days.values():
        cats = []
        day_total = 0
        for c in VOORMONTAGE_CATS:
            if sel != "alle" and c != sel:
                continue
            lines = [{"name": k, "qty": v} for k, v in d["cats"][c].items()]
            if lines:
                ct = sum(l["qty"] for l in lines)
                day_total += ct
                cats.append({"name": c, "total": ct, "lines": lines})
        if cats:
            day_list.append({"date": d["date"], "label": d["label"], "total": day_total, "cats": cats})
    return render_template("planning/voormonteren.html", days=day_list, today=today,
                           cats=VOORMONTAGE_CATS, sel=sel)


def _magazijn_overview(conn):
    """Live magazijnstatus: ingeplande orders (vanaf vandaag) met pick-/klaarzet-status,
    plus voormontage-voortgang per leverdag. Gedeeld met de Magazijn-app."""
    today = _today_iso()
    rows = conn.execute("""
        SELECT o.id AS oid, o.order_number AS onum, o.service_type AS service, p.date AS date,
               c.name AS client, o.delivery_address AS addr, o.city AS city,
               p.monteur_id AS monteur_id, mo.name AS monteur,
               (SELECT COUNT(*) FROM order_items WHERE order_id=o.id) AS n_items,
               (SELECT COUNT(*) FROM order_items WHERE order_id=o.id AND picked=1) AS n_picked,
               mg.gepickt_door AS gepickt_door, mg.gecontroleerd_door AS gecontroleerd_door,
               mg.klaargezet AS klaargezet, mg.picker_note AS picker_note,
               mg.manco AS manco, mg.manco_note AS manco_note, mg.manco_by AS manco_by
        FROM planning p JOIN orders o ON o.id=p.order_id
        LEFT JOIN clients c ON c.id=o.client_id
        LEFT JOIN monteurs mo ON mo.id=p.monteur_id
        LEFT JOIN order_magazijn mg ON mg.order_id=o.id
        WHERE p.date>=? AND p.status!='afgerond'
        ORDER BY p.date, p.sequence, o.order_number
    """, (today,)).fetchall()
    routepick = {(r["monteur_id"], r["date"]): r["status"] for r in
                 conn.execute("SELECT monteur_id,date,status FROM route_pick WHERE date>=?", (today,)).fetchall()}
    done = {(r["work_date"], r["item_name"]) for r in
            conn.execute("SELECT work_date,item_name FROM voormontage_done WHERE done=1").fetchall()}
    vm = conn.execute("""SELECT DISTINCT p.date AS date, oi.name AS name FROM planning p
                         JOIN orders o ON o.id=p.order_id JOIN order_items oi ON oi.order_id=o.id
                         WHERE p.date>=? AND p.status!='afgerond'""", (today,)).fetchall()
    vm_by_day = {}
    for r in vm:
        d = vm_by_day.setdefault(r["date"], {"total": 0, "done": 0})
        d["total"] += 1
        if (r["date"], r["name"]) in done:
            d["done"] += 1
    days, stat_klaar, stat_bezig, stat_vm_open, stat_manco = {}, 0, 0, 0, 0
    for r in rows:
        n_items, n_picked = (r["n_items"] or 0), (r["n_picked"] or 0)
        if r["klaargezet"]:
            pick = "klaar"
        elif n_picked and n_picked >= n_items and n_items > 0:
            pick = "gepickt"
        elif n_picked:
            pick = "bezig"
        else:
            pick = "todo"
        if r["klaargezet"]:
            stat_klaar += 1
        elif pick in ("bezig", "gepickt"):
            stat_bezig += 1
        if r["manco"]:
            stat_manco += 1
        d = days.setdefault(r["date"], {"date": r["date"], "label": _nl_date(r["date"]),
                                        "orders": [], "_monteurs": {}})
        d["orders"].append({**dict(r), "pick": pick})
        d["_monteurs"][r["monteur_id"] or 0] = (r["monteur"] or "Niet toegewezen")
    _RLBL = {"bezig": "Bezig met picken", "pauze": "Pauze", "klaar": "Picken klaar"}
    day_list = []
    for d in sorted(days.keys()):
        vmd = vm_by_day.get(d, {"total": 0, "done": 0})
        stat_vm_open += max(0, vmd["total"] - vmd["done"])
        routes = []
        for mid, nm in sorted(days[d]["_monteurs"].items(), key=lambda x: (x[1] or "")):
            st = routepick.get((mid, d), "niet_begonnen")
            routes.append({"monteur_id": mid, "name": nm, "status": st,
                           "label": _RLBL.get(st, "Niet begonnen")})
        day_list.append({"date": days[d]["date"], "label": days[d]["label"], "orders": days[d]["orders"],
                         "vm_total": vmd["total"], "vm_done": vmd["done"], "routes": routes})
    stats = {"orders": len(rows), "klaar": stat_klaar, "bezig": stat_bezig,
             "vm_open": stat_vm_open, "manco": stat_manco}
    return day_list, stats


@bp.route("/magazijn")
def magazijn():
    guard = login_required("view_magazijn")
    if guard:
        return guard
    u = current_user()
    conn = db()
    days, stats = _magazijn_overview(conn)
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec="minutes")
    tasks = conn.execute("""SELECT * FROM picker_tasks WHERE done=0 OR (done_at IS NOT NULL AND done_at>=?)
                            ORDER BY done, created_at DESC""", (cutoff,)).fetchall()
    pickers = conn.execute("SELECT id,name FROM users WHERE role='picker' AND active=1 ORDER BY name").fetchall()
    notes = []
    if u:
        notes = conn.execute("SELECT id,text,created_at FROM office_notifications WHERE recipient=? AND seen=0 "
                             "ORDER BY id DESC", (u["name"],)).fetchall()
        if notes:
            conn.execute("UPDATE office_notifications SET seen=1 WHERE recipient=? AND seen=0", (u["name"],))
            conn.commit()
    conn.close()
    return render_template("planning/magazijn_status.html", days=days, stats=stats, today=_today_iso(),
                           can_resolve=has_perm("view_magazijn"), tasks=tasks, pickers=pickers, notes=notes,
                           is_admin=(u["role"] == "beheerder" if u else False))


@bp.route("/magazijn/taak", methods=["POST"])
def magazijn_taak():
    guard = login_required("view_magazijn")
    if guard:
        return guard
    u = current_user()
    pid = request.form.get("picker_id")
    text = (request.form.get("text") or "").strip()[:200]
    if pid and text:
        conn = db()
        pk = conn.execute("SELECT name FROM users WHERE id=?", (pid,)).fetchone()
        conn.execute("""INSERT INTO picker_tasks(picker_id,picker_name,text,assigned_by,created_at,done)
                        VALUES(?,?,?,?,?,0)""",
                     (pid, (pk["name"] if pk else ""), text, (u["name"] if u else ""),
                      datetime.now().isoformat(timespec="minutes")))
        conn.commit()
        conn.close()
        flash("Taak toegewezen aan %s." % (pk["name"] if pk else "picker"))
    return redirect(url_for("planning.magazijn"))


@bp.route("/magazijn/taak/<int:tid>/verwijderen", methods=["POST"])
def magazijn_taak_delete(tid):
    guard = login_required("view_magazijn")
    if guard:
        return guard
    conn = db()
    conn.execute("DELETE FROM picker_tasks WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    flash("Taak verwijderd.")
    return redirect(url_for("planning.magazijn"))


_DEMO_ORDERS = [
    ("DEMO-1", "Gemeente Tilburg", "Stadhuisplein 130", "Tilburg",
     [("Se:Fit bureaustoel zwart", 4), ("Kastenwand eiken", 1)]),
    ("DEMO-2", "Studio Noord", "Overhoeksplein 1", "Amsterdam",
     [("Zit-sta bureau 160", 2), ("Verrijdbaar ladeblok", 3)]),
    ("DEMO-3", "Advocatenkantoor Rets", "Keizersgracht 12", "Amsterdam",
     [("Mast bureaustoel", 6), ("Akoestisch paneel", 2)]),
    ("DEMO-4", "De Bibliotheek", "Marktplein 5", "Breda",
     [("Bureau wit 140", 1), ("Verrijdbaar ladeblok", 2)]),
]


def _clear_magazijn_demo(conn):
    for r in conn.execute("SELECT id FROM orders WHERE order_number LIKE 'DEMO-%'").fetchall():
        oid = r["id"]
        conn.execute("DELETE FROM order_items WHERE order_id=?", (oid,))
        conn.execute("DELETE FROM planning WHERE order_id=?", (oid,))
        conn.execute("DELETE FROM order_magazijn WHERE order_id=?", (oid,))
        conn.execute("DELETE FROM orders WHERE id=?", (oid,))
    conn.execute("DELETE FROM picker_tasks WHERE assigned_by='Demo'")
    conn.execute("DELETE FROM route_pick")
    conn.execute("DELETE FROM voormontage_done WHERE done_by='Demo'")


@bp.route("/magazijn/demo", methods=["POST"])
def magazijn_demo():
    u = current_user()
    if not u or u["role"] != "beheerder":
        abort(403)
    conn = db()
    monteurs = conn.execute("SELECT id,name FROM monteurs WHERE active=1 ORDER BY id").fetchall()
    if not monteurs:
        conn.close()
        flash("Geen actieve monteurs — voeg er eerst een toe.")
        return redirect(url_for("planning.magazijn"))
    _clear_magazijn_demo(conn)
    today = datetime.now().date()
    t0, t1 = today.isoformat(), (today + timedelta(days=1)).isoformat()
    now = datetime.now().isoformat(timespec="minutes")
    cl = conn.execute("SELECT id FROM clients WHERE name='Demo Klant' LIMIT 1").fetchone()
    if cl:
        cid = cl["id"]
    else:
        cid = conn.execute("INSERT INTO clients(name,city,created_at) VALUES('Demo Klant','Breda',?)", (now,)).lastrowid
    made = []
    for i, (onum, client, addr, city, items) in enumerate(_DEMO_ORDERS):
        d = t0 if i < 2 else t1
        mid = monteurs[i % len(monteurs)]["id"]
        oid = conn.execute("""INSERT INTO orders(order_number,client_id,source,is_draft,status,delivery_address,
                              city,service_type,created_at) VALUES(?,?,?,0,'gepland',?,?,?,?)""",
                           (onum, cid, "demo", addr, city, "montage", now)).lastrowid
        for nm, qty in items:
            conn.execute("INSERT INTO order_items(order_id,name,qty) VALUES(?,?,?)", (oid, nm, qty))
        conn.execute("""INSERT INTO planning(order_id,monteur_id,date,sequence,status)
                        VALUES(?,?,?,?,'gepland')""", (oid, mid, d, i))
        made.append((oid, d, mid, items))
    conn.commit()
    # Voortgang zodat de statussen zichtbaar zijn
    first_mid = monteurs[0]["id"]
    conn.execute("""INSERT INTO route_pick(monteur_id,date,status,picker_name,started_at,updated_at)
                    VALUES(?,?,?,?,?,?)""", (first_mid, t0, "bezig", "Gurami", now, now))
    if made:
        oid0 = made[0][0]
        conn.execute("UPDATE order_items SET picked=1 WHERE order_id=?", (oid0,))
        conn.execute("""INSERT INTO order_magazijn(order_id,gepickt_door,gecontroleerd_door,klaargezet,
                        klaargezet_at,updated_at) VALUES(?,?,?,1,?,?)""",
                     (oid0, "Gurami", "Tim", now, now))
    if len(made) > 1:
        conn.execute("""INSERT INTO order_magazijn(order_id,manco,manco_note,manco_by,updated_at)
                        VALUES(?,1,?,?,?)""",
                     (made[1][0], "1 bureaustoel beschadigd aangeleverd", "Gurami", now))
    # Voormontage: helft van vandaag afvinken
    vmnames = [r["name"] for r in conn.execute("""SELECT DISTINCT oi.name AS name FROM planning p
                 JOIN orders o ON o.id=p.order_id JOIN order_items oi ON oi.order_id=o.id
                 WHERE p.date=? AND p.status!='afgerond'""", (t0,)).fetchall()]
    for i, nm in enumerate(vmnames):
        if i % 2 == 0:
            conn.execute("""INSERT INTO voormontage_done(work_date,item_name,done,done_by,done_at)
                            VALUES(?,?,1,'Demo',?)""", (t0, nm, now))
    # Taken
    pk = conn.execute("SELECT id,name FROM users WHERE role='picker' AND active=1 ORDER BY id LIMIT 1").fetchone()
    if pk:
        for txt in ["Retour Studio Noord uitpakken en controleren",
                    "Voorraad bureaustoelen bijvullen uit magazijn 2"]:
            conn.execute("""INSERT INTO picker_tasks(picker_id,picker_name,text,assigned_by,created_at,done)
                            VALUES(?,?,?,'Demo',?,0)""", (pk["id"], pk["name"], txt, now))
    conn.commit()
    conn.close()
    flash("Magazijn gevuld met testdata (routes, picken, 1 manco, voormontage, taken)."
          + ("" if pk else " Let op: nog geen picker-account — maak die aan onder Gebruikers voor de taken."))
    return redirect(url_for("planning.magazijn"))


@bp.route("/magazijn/demo-clear", methods=["POST"])
def magazijn_demo_clear():
    u = current_user()
    if not u or u["role"] != "beheerder":
        abort(403)
    conn = db()
    _clear_magazijn_demo(conn)
    conn.commit()
    conn.close()
    flash("Magazijn-testdata gewist.")
    return redirect(url_for("planning.magazijn"))


@bp.route("/magazijn/manco/vrijgeven", methods=["POST"])
def magazijn_manco_resolve():
    guard = login_required("view_magazijn")
    if guard:
        return guard
    u = current_user()
    oid = request.form.get("order_id")
    if oid:
        conn = db()
        conn.execute("UPDATE order_magazijn SET manco=0, manco_resolved_by=?, manco_resolved_at=? WHERE order_id=?",
                     ((u["name"] if u else ""), datetime.now().isoformat(timespec="minutes"), oid))
        conn.commit()
        conn.close()
        flash("Manco vrijgegeven — de picker kan de order weer klaarzetten.")
    return redirect(url_for("planning.magazijn"))


_PICKER_DEFAULTS = [
    ("Gurami", "gurami@office-interior.nl"),
    ("Tim", "tim@office-interior.nl"),
    ("Gregorz", "gregorz@office-interior.nl"),
    ("Stijn Pas", "stijnpas@office-interior.nl"),
]
_PICKER_DEFAULT_PW = "Magazijn2026!"


@bp.route("/users/picker-defaults", methods=["POST"])
def users_picker_defaults():
    """Snel de standaard picker-accounts aanmaken (vanaf de Gebruikers-pagina)."""
    guard = login_required("manage_users")
    if guard:
        return guard
    conn = db()
    made = []
    for name, email in _PICKER_DEFAULTS:
        if not conn.execute("SELECT id FROM users WHERE lower(email)=?", (email.lower(),)).fetchone():
            conn.execute("""INSERT INTO users(name,email,password,role,permissions,active,created_at)
                            VALUES(?,?,?,?,?,1,?)""",
                         (name, email.lower(), _hash_pw(_PICKER_DEFAULT_PW), "picker",
                          json.dumps(list(ROLE_DEFAULTS["picker"])), _today_iso()))
            made.append(name)
    conn.commit()
    conn.close()
    flash("Picker-accounts aangemaakt: %s. Wachtwoord: %s (laat ze dit wijzigen)."
          % (", ".join(made) if made else "geen nieuwe (bestonden al)", _PICKER_DEFAULT_PW))
    return redirect(url_for("planning.users"))


def _picklist_orders(conn, date):
    """Alle ingeplande (niet-afgeronde) orders van een dag, met hun orderregels."""
    orders = conn.execute("""
        SELECT o.id AS oid, o.order_number AS onum, o.delivery_address AS addr, o.city AS city,
               o.postal AS postal, o.invoice_address AS invoice_address,
               o.service_type AS service, o.instructions AS instructions, o.phone AS phone,
               c.name AS client, c.invoice_address AS client_invoice,
               p.slot_start AS slot_start, p.slot_end AS slot_end,
               p.sequence AS seq, m.name AS monteur, b.name AS bus
        FROM planning p
        JOIN orders o ON o.id = p.order_id
        LEFT JOIN clients c ON c.id = o.client_id
        LEFT JOIN monteurs m ON m.id = p.monteur_id
        LEFT JOIN busses b ON b.id = p.bus_id
        WHERE p.date = ? AND p.status != 'afgerond'
        ORDER BY p.sequence, o.order_number
    """, (date,)).fetchall()
    out = []
    for o in orders:
        items = conn.execute("SELECT name, qty FROM order_items WHERE order_id=? ORDER BY id",
                             (o["oid"],)).fetchall()
        out.append({**dict(o), "lines": [dict(it) for it in items]})
    return out


@bp.route("/picklijst")
def picklijst():
    guard = login_required("view_preassembly")
    if guard:
        return guard
    date = request.args.get("date") or _today_iso()
    conn = db()
    orders = _picklist_orders(conn, date)
    conn.close()
    return render_template("planning/picklijst.html", orders=orders, date=date,
                           date_label=_nl_date(date), today=_today_iso())


@bp.route("/picklijst/print")
def picklijst_print():
    guard = login_required("view_preassembly")
    if guard:
        return guard
    date = request.args.get("date") or _today_iso()
    conn = db()
    orders = _picklist_orders(conn, date)
    conn.close()
    return render_template("planning/picklijst_print.html", orders=orders, date=date,
                           date_label=_nl_date(date), HOME_BASE=HOME_BASE)


# --------------------------------------------------------------------------- #
#  Documenten: informatieformulieren e.d. (opgeslagen in de database)
# --------------------------------------------------------------------------- #
MAX_DOC_BYTES = 15 * 1024 * 1024  # 15 MB per bestand


def _human_size(n):
    n = n or 0
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


@bp.route("/documenten")
def documenten():
    guard = login_required("view_documents")
    if guard:
        return guard
    conn = db()
    rows = conn.execute("""SELECT id, title, description, filename, mimetype, size, uploaded_by, uploaded_at
                           FROM documents ORDER BY id DESC""").fetchall()
    conn.close()
    docs = [{**dict(r), "size_h": _human_size(r["size"])} for r in rows]
    return render_template("planning/documenten.html", docs=docs)


@bp.route("/documenten/upload", methods=["POST"])
def documenten_upload():
    if not has_perm("view_documents"):
        abort(403)
    f = request.files.get("file")
    title = (request.form.get("title") or "").strip()
    desc = (request.form.get("description") or "").strip()
    if not f or not f.filename:
        flash("Kies een bestand om te uploaden.")
        return redirect(url_for("planning.documenten"))
    data = f.read()
    if len(data) > MAX_DOC_BYTES:
        flash("Bestand is te groot (max 15 MB).")
        return redirect(url_for("planning.documenten"))
    fname = secure_filename(f.filename) or "document"
    if not title:
        title = fname
    u = current_user()
    uploader = (request.form.get("uploaded_by") or "").strip() or (u["name"] if u else "")
    conn = db()
    conn.execute("""INSERT INTO documents(title,description,filename,mimetype,size,data,uploaded_by,uploaded_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                 (title, desc, fname, (f.mimetype or "application/octet-stream"), len(data),
                  data, uploader, datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()
    flash("Document geüpload.")
    return redirect(url_for("planning.documenten"))


@bp.route("/documenten/<int:did>")
def documenten_download(did):
    guard = login_required("view_documents")
    if guard:
        return guard
    conn = db()
    row = conn.execute("SELECT filename, mimetype, data FROM documents WHERE id=?", (did,)).fetchone()
    conn.close()
    if not row or row["data"] is None:
        abort(404)
    data = bytes(row["data"])
    disp = "inline" if request.args.get("inline") else "attachment"
    fname = (row["filename"] or "document").replace('"', "")
    return Response(data, mimetype=(row["mimetype"] or "application/octet-stream"),
                    headers={"Content-Disposition": '%s; filename="%s"' % (disp, fname)})


@bp.route("/documenten/<int:did>/verwijderen", methods=["POST"])
def documenten_delete(did):
    if not has_perm("view_documents"):
        abort(403)
    conn = db()
    conn.execute("DELETE FROM documents WHERE id=?", (did,))
    conn.commit()
    conn.close()
    flash("Document verwijderd.")
    return redirect(url_for("planning.documenten"))


# --------------------------------------------------------------------------- #
#  Openbaar kladblok: gedeelde, real-time aantekeningen (iedereen ziet/bewerkt)
# --------------------------------------------------------------------------- #
@bp.route("/handleiding")
def handleiding():
    guard = login_required("view_documents")
    if guard:
        return guard
    return render_template("planning/handleiding.html")


@bp.route("/kladblok")
def kladblok():
    guard = login_required("view_documents")
    if guard:
        return guard
    u = current_user()
    conn = db()
    row = conn.execute("SELECT content, updated_by, updated_at FROM notepad WHERE id=1").fetchone()
    log = []
    if u["role"] == "beheerder":
        log = [dict(r) for r in conn.execute(
            "SELECT person, ts FROM notepad_log ORDER BY id DESC LIMIT 40").fetchall()]
    conn.close()
    return render_template("planning/kladblok.html",
                           content=(row["content"] if row else "") or "",
                           by=(row["updated_by"] if row else ""),
                           at=(row["updated_at"] if row else ""),
                           is_admin=(u["role"] == "beheerder"), log=log)


@bp.route("/kladblok/poll")
def kladblok_poll():
    if not has_perm("view_documents"):
        return jsonify(ok=False), 403
    conn = db()
    row = conn.execute("SELECT content, updated_by, updated_at FROM notepad WHERE id=1").fetchone()
    conn.close()
    return jsonify(ok=True, content=(row["content"] if row else "") or "",
                   by=(row["updated_by"] if row else ""), at=(row["updated_at"] if row else ""))


@bp.route("/kladblok/save", methods=["POST"])
def kladblok_save():
    if not has_perm("view_documents"):
        return jsonify(ok=False), 403
    u = current_user()
    content = (request.form.get("content") or "")[:50000]
    now = datetime.now().isoformat(timespec="minutes")
    conn = db()
    conn.execute("""INSERT INTO notepad(id,content,updated_by,updated_at) VALUES(1,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET content=excluded.content,
                    updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                 (content, u["name"], now))
    conn.execute("INSERT INTO notepad_log(person,ts,content) VALUES(?,?,?)", (u["name"], now, content))
    # alleen de laatste 100 bewerkingen bewaren
    conn.execute("DELETE FROM notepad_log WHERE id NOT IN "
                 "(SELECT id FROM notepad_log ORDER BY id DESC LIMIT 100)")
    conn.commit()
    conn.close()
    return jsonify(ok=True, by=u["name"], at=now)


# --------------------------------------------------------------------------- #
#  Commandocentrum: live status van alle koppelingen + AI-advies bij storing
# --------------------------------------------------------------------------- #
def _conn_advice(key):
    """Slim, regelgebaseerd advies bij een storing (diagnose + concrete stappen)."""
    A = {
        "db": ("Het lijkt erop dat de database niet reageert — dan werkt vrijwel niets meer.",
               ["Controleer of de PostgreSQL-database (Render) actief is.",
                "Kijk of DATABASE_URL nog klopt op beide services.",
                "Start de service opnieuw via Render → Manual Deploy."]),
        "resend_cfg": ("E-mail is nog niet ingesteld, dus 2FA-codes en klantmails worden niet verstuurd.",
               ["Ga naar Instellingen → Koppelingen → E-mail.",
                "Vul de Resend API-sleutel en het afzendadres in.",
                "Zet ‘E-mails écht versturen’ aan."]),
        "resend_test": ("E-mail staat in testmodus — er gaat niets daadwerkelijk de deur uit.",
               ["Open Instellingen → Koppelingen → E-mail.",
                "Zet ‘E-mails écht versturen’ aan zodra je live wilt."]),
        "resend_domain": ("Het lijkt erop dat uitgaande mail niet aankomt doordat het afzenddomein nog niet is geverifieerd bij Resend.",
               ["Voeg de SPF- en DKIM-records toe aan de DNS van office-interior.com.",
                "Wacht tot Resend het domein op ‘Verified’ zet.",
                "Doe daarna een testmail via ‘Test verbinding’."]),
        "shopify": ("Shopify is nog niet gekoppeld; nieuwe orders komen niet automatisch binnen.",
               ["Maak in Shopify een webhook ‘Bestelling aangemaakt’ naar /api/shopify/webhook.",
                "Kopieer het webhook-secret naar Instellingen → Koppelingen → Shopify."]),
        "gps": ("Er is nog geen live locatie binnengekomen van de monteurs.",
               ["Laat de monteur in de app de locatie-deling aanzetten (tab ‘Ik’).",
                "Controleer of de telefoon locatietoegang geeft aan de app."]),
        "velocity": ("Er zijn nog geen busgegevens uit Velocity.",
               ["Controleer de busregistratie-koppeling (Velocity).",
                "Voeg voertuigen toe onder Bussen."]),
        "backend": ("Het lijkt erop dat er recent fouten optraden in de OfficeRoute-software zelf.",
               ["Probeer de pagina waar het misging opnieuw te openen.",
                "Bekijk de logs van de Render-service voor de exacte foutmelding.",
                "Blijft het terugkomen? Meld de melding zodat het opgelost wordt."]),
        "app_off": ("Er komt geen live verbinding binnen vanuit de monteurs-app.",
               ["Laat een monteur inloggen in de app en zijn route openen.",
                "Zet locatie-deling aan in de app (tab ‘Ik’)."]),
        "google": ("De Google-koppeling (OAuth/MFA · Maps · GPS) is nog niet actief.",
               ["Maak een Google Cloud-project met OAuth-client aan.",
                "Zet de Google-sleutel in ‘Koppelingen instellen’."]),
        "maps": ("Google Maps is nog niet als koppeling actief.",
               ["Koppel Google Maps via de Google-sleutel in ‘Koppelingen instellen’."]),
        "waze": ("Waze is nog niet gekoppeld.",
               ["Waze werkt nu via deeplinks; een directe koppeling is optioneel."]),
        "oauth": ("Inloggen via Google (OAuth + MFA) is nog niet ingesteld.",
               ["Configureer een Google OAuth-client en koppel die in ‘Koppelingen instellen’."]),
        "traffic": ("Live verkeers-/file-info is nog niet gekoppeld; nu wordt een voorbeeld getoond.",
               ["Koppel de open verkeersdata van NDW (Nationale Databank Wegverkeersgegevens).",
                "Vul de NDW-sleutel in bij ‘Koppelingen instellen’."]),
        "transip": ("Het eigen domein (TransIP) is nog niet aan de services gekoppeld; OfficeRoute draait nu op de Render-adressen.",
               ["Registreer/gebruik het domein bij TransIP.",
                "Voeg per service een Custom Domain toe in Render en zet de DNS-records bij TransIP.",
                "Render regelt daarna automatisch het SSL-certificaat (https)."]),
    }
    d = A.get(key)
    return {"diagnose": d[0], "stappen": d[1]} if d else None


def _resend_domain_ok(key, from_email):
    """Diepe check: is het afzenddomein geverifieerd bij Resend? (korte timeout)."""
    dom = from_email.split("@")[-1].lower() if "@" in from_email else ""
    if not dom:
        return False, "Ongeldig afzendadres"
    try:
        req = urllib.request.Request("https://api.resend.com/domains",
                                     headers={"Authorization": "Bearer " + key})
        data = json.loads(urllib.request.urlopen(req, timeout=6).read().decode("utf-8"))
        doms = data.get("data") or data.get("domains") or []
        for d in doms:
            if (d.get("name") or "").lower() == dom:
                if (d.get("status") or "").lower() == "verified":
                    return True, "Domein geverifieerd"
                return False, "Domein %s nog niet geverifieerd (%s)" % (dom, d.get("status") or "onbekend")
        return False, "Domein %s niet gevonden in Resend" % dom
    except Exception:
        return False, "Kon Resend niet bereiken"


def _connection_health(deep=False):
    """Bepaal de live status van elke koppeling. deep=True doet ook netwerk-checks."""
    st = {}

    def put(k, status, detail, sync="", advice=None):
        st[k] = {"status": status, "detail": detail, "sync": sync, "advice": advice}

    conn = db()
    put("render", "ok", "Hosting online", "nu")

    # Backend (Python) — recente onafgehandelde fouten?
    recent = [e for e in list(_APP_ERRORS) if time.time() - e["ts"] < 3600]
    if recent:
        put("backend", "err",
            "%d fout%s · laatste: %s" % (len(recent), "en" if len(recent) > 1 else "", recent[-1]["err"][:50]),
            advice=_conn_advice("backend"))
    else:
        put("backend", "ok", "Geen fouten")

    try:
        conn.execute("SELECT 1").fetchone()
        put("db", "ok", "Verbinding actief", "nu")
    except Exception:
        put("db", "err", "Geen databaseverbinding", advice=_conn_advice("db"))

    # E-mail / Resend — alleen groen als écht ingesteld + live (+ domein geverifieerd)
    try:
        c = _email_cfg()
    except Exception:
        c = {}
    rkey = (c.get("resend_api_key") or "").strip()
    frm = (c.get("from_email") or c.get("smtp_user") or "").strip()
    live = (c.get("send_live") or "0") == "1"
    if not (rkey and frm and live):
        put("resend", "warn", "In te stellen", advice=_conn_advice("resend_cfg"))
    elif deep:
        ok, msg = _resend_domain_ok(rkey, frm)
        put("resend", "ok" if ok else "err", msg,
            advice=None if ok else _conn_advice("resend_domain"))
    else:
        put("resend", "ok", "Actief", "live")

    # Shopify — groen alleen met webhook-secret
    try:
        sh = {r["field"]: r["value"] for r in
              conn.execute("SELECT field,value FROM integrations WHERE ikey='shopify'").fetchall()}
    except Exception:
        sh = {}
    if (sh.get("webhook_secret") or "").strip():
        put("shopify", "ok", "Webhook gekoppeld")
    else:
        put("shopify", "err", "Niet gekoppeld", advice=_conn_advice("shopify"))

    # Monteurs-app + live GPS — groen alleen bij ECHT live locatie (laatste 30 min)
    cutoff = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="minutes")
    try:
        rec = conn.execute("SELECT MAX(updated_at) AS m FROM monteur_location WHERE live=1").fetchone()
        last = rec["m"] if rec else None
    except Exception:
        last = None
    # Apps draaien en delen de gedeelde database (gedeployed)
    put("app", "ok", "Verbonden", "live")
    put("magazijn", "ok", "Verbonden", "live")
    # Live GPS — groen alleen bij ECHT live locatie (laatste 30 min)
    if last and last >= cutoff:
        put("gps1", "ok", "Live locatie ontvangen")
    else:
        put("gps1", "err", "Geen live locatie", advice=_conn_advice("gps"))
    # Navigatie (Maps/Waze) — geen datakoppeling, alleen deeplinks
    put("maps", "link", "Navigatie-link (geen data)")
    # Eigen domein via TransIP — nog te koppelen
    put("transip", "warn", "Nog te koppelen", advice=_conn_advice("transip"))
    # Nog niet gekoppeld
    put("oauth", "err", "Niet gekoppeld", advice=_conn_advice("oauth"))
    put("traffic", "err", "Niet gekoppeld · NDW", advice=_conn_advice("traffic"))
    put("velocity", "err", "Niet gekoppeld", advice=_conn_advice("velocity"))

    conn.close()
    return st


@bp.route("/koppelingen")
def koppelingen():
    guard = login_required("view_connections")
    if guard:
        return guard
    return render_template("planning/koppelingen.html",
                           health=_connection_health(deep=False),
                           can_act=has_perm("manage_integrations"))


@bp.route("/koppelingen/check")
def koppelingen_check():
    if not has_perm("manage_integrations"):
        abort(403)
    return jsonify(_connection_health(deep=True))


# Demodata opnieuw vullen met de datum van vandaag (beheerder-only, met bevestiging).
# Leegt de demo-inhoud (NIET de koppelingen/instellingen) en draait _seed opnieuw.
_RESEED_TABLES = ["bus_issues", "deliveries", "planning", "order_items", "orders",
                  "clients", "route_closed", "free_days", "email_log", "monteur_location",
                  "office_days", "vehicle_km", "team_questions", "chat_messages", "leave_requests",
                  "busses", "monteurs", "users"]


_SLOTS_DEMO = [("08:00", "09:15"), ("09:30", "10:45"), ("11:00", "12:15"),
               ("13:00", "14:15"), ("14:30", "15:45"), ("16:00", "17:15")]


@bp.route("/admin/demo-planning", methods=["POST"])
def admin_demo_planning():
    """Vul de dagplanning van vandaag met demo-leveringen (beheerder, niet-destructief)."""
    u = current_user()
    if not u or u["role"] != "beheerder":
        abort(403)
    conn = db()
    today = _today_iso()
    monteurs = [m["id"] for m in conn.execute("SELECT id FROM monteurs WHERE active=1 ORDER BY id").fetchall()]
    buses = [b["id"] for b in conn.execute("SELECT id FROM busses WHERE active=1 ORDER BY id").fetchall()]
    if not monteurs:
        conn.close()
        flash("Geen actieve monteurs om mee te plannen.")
        return redirect(url_for("planning.dashboard"))
    orders = conn.execute("""SELECT id FROM orders WHERE is_draft=0
                             AND status IN('in_te_plannen','gepland') ORDER BY amount DESC LIMIT 18""").fetchall()
    seqby = {}
    n = 0
    for i, o in enumerate(orders):
        mid = monteurs[i % len(monteurs)]
        bid = (buses[i % len(buses)] if buses else None)
        seq = seqby.get(mid, 0)
        seqby[mid] = seq + 1
        ss, es = _SLOTS_DEMO[seq % len(_SLOTS_DEMO)]
        conn.execute("""INSERT INTO planning(order_id,monteur_id,bus_id,date,slot_start,slot_end,sequence,status)
                        VALUES(?,?,?,?,?,?,?,'gepland')
                        ON CONFLICT(order_id) DO UPDATE SET monteur_id=excluded.monteur_id,bus_id=excluded.bus_id,
                        date=excluded.date,slot_start=excluded.slot_start,slot_end=excluded.slot_end,
                        sequence=excluded.sequence,status='gepland'""",
                     (o["id"], mid, bid, today, ss, es, seq))
        conn.execute("UPDATE orders SET status='gepland' WHERE id=?", (o["id"],))
        n += 1
    conn.commit()
    conn.close()
    flash("Dagplanning gevuld met %d demo-leveringen voor vandaag." % n)
    return redirect(url_for("planning.planning"))


@bp.route("/admin/reseed-demo", methods=["POST"])
def admin_reseed_demo():
    u = current_user()
    if not u or u["role"] != "beheerder":
        abort(403)
    if (request.form.get("confirm") or "") != "WIS-EN-VUL":
        abort(400)
    conn = db()
    for t in _RESEED_TABLES:
        try:
            conn.execute(("TRUNCATE %s RESTART IDENTITY" if IS_PG else "DELETE FROM %s") % t)
        except Exception:
            pass
    if not IS_PG:
        try:
            conn.execute("DELETE FROM sqlite_sequence")
        except Exception:
            pass
    conn.commit()
    _seed(conn)
    conn.commit()
    conn.close()
    session.clear()
    flash("Demodata opnieuw gevuld met de datum van vandaag. Log opnieuw in.")
    return redirect(url_for("planning.login"))


@bp.route("/admin/wis-testorders", methods=["POST"])
def admin_wipe_testdata():
    """Verwijder alle test-/demo-orders en bijbehorende magazijn-/planningsdata,
    zodat er vanaf nu alleen echte (Shopify-)orders binnenkomen. Beheerder-only."""
    u = current_user()
    if not u or u["role"] != "beheerder":
        abort(403)
    if (request.form.get("confirm") or "") != "WIS-TESTDATA":
        abort(400)
    conn = db()
    for stmt in ("DELETE FROM order_items", "DELETE FROM order_magazijn", "DELETE FROM planning",
                 "DELETE FROM voormontage_done", "DELETE FROM route_pick", "DELETE FROM deliveries",
                 "DELETE FROM picker_tasks", "DELETE FROM office_notifications",
                 "DELETE FROM orders", "DELETE FROM clients"):
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.commit()
    conn.close()
    flash("Alle test-/demo-orders en bijbehorende data zijn gewist. Vanaf nu verschijnen hier alleen nieuwe (echte) orders.")
    return redirect(url_for("planning.orders"))


# --------------------------------------------------------------------------- #
#  Shopify — realtime orderimport via webhook (orders/create)
# --------------------------------------------------------------------------- #
def _shopify_cfg():
    conn = db()
    cfg = {r["field"]: r["value"] for r in
           conn.execute("SELECT field,value FROM integrations WHERE ikey=?", ("shopify",)).fetchall()}
    conn.close()
    return cfg


DESK_MODELS = ["Fuse", "Pinta", "Duo", "Now", "Aero", "Rosa"]
_BLAD_SHORT = {
    "robuust eiken": "RobEik", "bruin eiken": "BruinEik", "midden eiken": "MidEik",
    "licht eiken": "LichtEik", "donker eiken": "DonkerEik", "wit eiken": "WitEik",
    "zwart eiken": "ZwartEik", "naturel eiken": "NatEik",
}


def _blad_short(s):
    """Korte bladkleur voor snelle herkenning (bv. 'Robuust eiken' -> 'RobEik')."""
    k = (s or "").strip().lower()
    return _BLAD_SHORT.get(k, (s or "").strip().replace(" ", ""))


def _clean_item_name(li):
    """Nette, herkenbare artikelnaam voor de planners.
    - Bureaus (Fuse/Pinta/Duo/Now/Aero/Rosa): 'Model Blad/Frame/Maat', bv. 'Fuse RobEik/Zwart/160'.
    - Stoelen/overig: merk 'Renab' weghalen (bv. 'Renab Japandi' -> 'Japandi')."""
    title = (li.get("title") or li.get("name") or "Artikel").strip()
    variant = (li.get("variant_title") or "").strip()
    words = title.lower().split()
    model = next((mdl for mdl in DESK_MODELS if mdl.lower() in words), None)
    if model:
        parts = [p.strip() for p in variant.split("/") if p.strip()] if variant else []
        if parts:
            parts[0] = _blad_short(parts[0])           # blad afkorten
            return "%s %s" % (model, "/".join(parts))
        return model
    if title.lower().startswith("renab "):
        title = title[6:].strip()
    return title


def _int(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


def _flt(v, d=0.0):
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return d


def _parse_next_link(link_header):
    """Haal de 'next'-URL uit een Shopify Link-header (cursor-paginatie)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        seg = part.split(";")
        if len(seg) >= 2 and 'rel="next"' in seg[1]:
            return seg[0].strip().strip("<>").strip()
    return None


def _shopify_products_import(conn, shop, token):
    """Importeer alle ACTIEVE Shopify-producten als artikelen (titel = weergavenaam,
    eerste woord = herkenningsnaam, montagetijden op 0). Dedupe op naam/weergavenaam.
    Retourneert (toegevoegd, overgeslagen, foutmelding-of-None)."""
    shop = (shop or "").strip().replace("https://", "").replace("http://", "").strip("/")
    token = (token or "").strip()
    if not shop or not token:
        return (0, 0, "Vul eerst de Shop-URL én het Admin API-token in bij Koppelingen → Shopify.")
    existing = set()
    for r in conn.execute("SELECT name, display_name FROM products").fetchall():
        if r["name"]:
            existing.add(r["name"].strip().lower())
        if r["display_name"]:
            existing.add(r["display_name"].strip().lower())
    url = "https://%s/admin/api/2024-04/products.json?status=active&limit=250" % shop
    added = skipped = 0
    try:
        for _ in range(60):  # veiligheidscap
            req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": token,
                                                       "Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=25)
            payload = json.loads(resp.read().decode("utf-8"))
            for p in payload.get("products", []):
                if (p.get("status") or "active") != "active":
                    continue
                title = (p.get("title") or "").strip()
                if not title:
                    continue
                if title.lower() in existing:
                    skipped += 1
                    continue
                rec = (title.split()[0] if title.split() else title).lower()
                grams = 0
                for v in (p.get("variants") or []):
                    if v.get("grams"):
                        grams = v["grams"]
                        break
                wkg = round(grams / 1000.0, 1) if grams else 0
                conn.execute("INSERT INTO products(name,display_name,weight_kg,active,created_at) VALUES(?,?,?,1,?)",
                             (rec, title, wkg, _today_iso()))
                existing.add(title.lower())
                added += 1
            nxt = _parse_next_link(resp.headers.get("Link") or resp.headers.get("link"))
            if not nxt:
                break
            url = nxt
        conn.commit()
        return (added, skipped, None)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:200]
        except Exception:
            detail = e.reason
        return (added, skipped, "Shopify weigerde (code %s): %s" % (e.code, detail))
    except Exception as e:
        return (added, skipped, "Import mislukt: %s" % e)


def _load_products(conn):
    try:
        rows = conn.execute("SELECT name,display_name,m1,m2,m3,m4,m5,l1,l2,l3,l4,l5,weight_kg FROM products WHERE active=1").fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({"name": (r["name"] or "").strip().lower(),
                    "display": (r["display_name"] or "").strip(),
                    "tiers": [r["m1"] or 0, r["m2"] or 0, r["m3"] or 0, r["m4"] or 0, r["m5"] or 0],
                    "tiers_lev": [r["l1"] or 0, r["l2"] or 0, r["l3"] or 0, r["l4"] or 0, r["l5"] or 0],
                    "weight": r["weight_kg"] or 0})
    return out


def _match_product(item_name, products):
    nl = (item_name or "").lower()
    words = nl.replace("/", " ").split()
    for p in products:
        pn = p["name"]
        if pn and (pn in words or nl.startswith(pn)):
            return p
    return None


def _montage_for_qty(tiers, qty):
    """Montagetijd voor een aantal stuks o.b.v. de 1-5-tarieven.
    >5 stuks = zoveel volle blokken van 5 (× tarief-5) + de rest volgens het reststuk-tarief.
    Lege tarieven vallen lineair terug op het 1-stuks-tarief."""
    if qty <= 0:
        return 0
    m1 = tiers[0] or 0

    def tier(n):   # tijd voor n stuks (1..5)
        return tiers[n - 1] if tiers[n - 1] else m1 * n
    if qty <= 5:
        return tier(qty)
    full, rem = divmod(qty, 5)
    return tier(5) * full + (tier(rem) if rem else 0)


def _order_montage(items, products, fallback=0, service_type="montage"):
    """Werkdruk-minuten voor een order. 'montage' gebruikt de volle montagetijden;
    'levering' (ongemonteerd) en 'ophalen' de lichte tijden (tiers_lev). Is er geen
    aparte levertijd ingevuld, dan valt het terug op de montagetijd."""
    lev = (service_type or "montage") != "montage"
    total, counted = 0, False
    for it in items:
        p = _match_product(it["name"], products)
        if p:
            tiers = p.get("tiers_lev") if lev else p["tiers"]
            if not tiers or not any(tiers):
                tiers = p["tiers"]
            total += _montage_for_qty(tiers, int(it["qty"] or 1))
            counted = True
        else:
            # Maatwerk-regel: gebruik de handmatig ingevulde tijd (totaal voor die regel).
            mc = it.get("montage_custom")
            if mc:
                total += int(mc)
                counted = True
    return total if counted else fallback


def _order_weight(items, products, fallback=0):
    """Totaalgewicht (kg) van een order uit de artikelgewichten (qty x kg/stuk).
    Valt terug op het meegegeven fallback-gewicht als geen enkel artikel matcht."""
    total, matched = 0.0, False
    for it in items:
        p = _match_product(it["name"], products)
        if p and (p.get("weight") or 0):
            total += float(p["weight"]) * int(it["qty"] or 1)
            matched = True
    return total if matched else fallback


def _maatwerk_alerts_on():
    # Waarschuwingen pas tonen zodra de artikelen + tijden zijn ingericht (anders is
    # ELKE order 'maatwerk'). Aan/uit op de Artikelen-pagina. Standaard UIT.
    return setting("maatwerk_alerts", "0") == "1"


def _orders_needing_custom():
    """Orders (niet-draft, niet afgerond) met één of meer MAATWERK-regels waarvoor de
    montagetijd nog niet is ingevuld (regel matcht geen artikel én montage_custom IS NULL).
    Alleen actief als de maatwerk-waarschuwingen aan staan."""
    if not _maatwerk_alerts_on():
        return []
    conn = db()
    try:
        prods = _load_products(conn)
        rows = conn.execute("""SELECT o.id AS oid, o.order_number AS onum, c.name AS client
                               FROM orders o LEFT JOIN clients c ON c.id=o.client_id
                               WHERE o.is_draft=0 AND o.status!='afgerond'""").fetchall()
        if not rows:
            return []
        oids = [r["oid"] for r in rows]
        by = {}
        for it in conn.execute("SELECT order_id,name,montage_custom FROM order_items WHERE order_id IN (%s)"
                               % ",".join("?" * len(oids)), tuple(oids)).fetchall():
            by.setdefault(it["order_id"], []).append(it)
        out = []
        for r in rows:
            n = sum(1 for it in by.get(r["oid"], [])
                    if it["montage_custom"] is None and not _match_product(it["name"], prods))
            if n:
                out.append({"oid": r["oid"], "onum": r["onum"], "client": r["client"], "n": n})
        return out
    finally:
        conn.close()


@bp.route("/products", methods=["GET", "POST"])
def products():
    guard = login_required("manage_settings")
    if guard:
        return guard
    conn = db()
    if request.method == "POST":
        act = request.form.get("action")
        if act == "add":
            name = (request.form.get("name") or "").strip()
            if name:
                conn.execute("""INSERT INTO products(name,display_name,weight_kg,m1,m2,m3,m4,m5,l1,l2,l3,l4,l5,active,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                             (name, (request.form.get("display_name") or "").strip(),
                              _flt(request.form.get("weight_kg")),
                              _int(request.form.get("m1")), _int(request.form.get("m2")), _int(request.form.get("m3")),
                              _int(request.form.get("m4")), _int(request.form.get("m5")),
                              _int(request.form.get("l1")), _int(request.form.get("l2")), _int(request.form.get("l3")),
                              _int(request.form.get("l4")), _int(request.form.get("l5")), _today_iso()))
                conn.commit()
                flash("Artikel toegevoegd.")
            else:
                flash("Geef een (herkennings)naam op.")
        elif act == "seed_desks":
            n = 0
            for mdl in DESK_MODELS:
                if not conn.execute("SELECT 1 FROM products WHERE lower(name)=?", (mdl.lower(),)).fetchone():
                    conn.execute("INSERT INTO products(name,active,created_at) VALUES(?,1,?)", (mdl, _today_iso()))
                    n += 1
            conn.commit()
            flash("%d standaard bureaus toegevoegd — vul de montagetijden nog aan." % n)
        elif act == "toggle_maatwerk":
            val = "1" if request.form.get("maatwerk_alerts") else "0"
            conn.execute("INSERT INTO settings(skey,value) VALUES('maatwerk_alerts',?) "
                         "ON CONFLICT(skey) DO UPDATE SET value=excluded.value", (val,))
            conn.commit()
            flash("Maatwerk-waarschuwingen " + ("aangezet." if val == "1" else "uitgezet."))
        elif act == "import_shopify":
            sc = _shopify_cfg()
            added, skipped, err = _shopify_products_import(conn, sc.get("shop_url"), sc.get("access_token"))
            if err:
                flash("Shopify-import: " + err)
            else:
                flash("Shopify-import klaar: %d actieve artikelen toegevoegd, %d al aanwezig. Vul nu de tijden aan." % (added, skipped))
        conn.close()
        return redirect(url_for("planning.products"))
    rows = conn.execute("SELECT * FROM products ORDER BY active DESC, name").fetchall()
    conn.close()
    return render_template("planning/products.html", products=rows,
                           maatwerk_on=(setting("maatwerk_alerts", "0") == "1"))


@bp.route("/products/<int:pid>/edit", methods=["POST"])
def product_edit(pid):
    guard = login_required("manage_settings")
    if guard:
        return guard
    f = request.form
    conn = db()
    conn.execute("""UPDATE products SET name=?,display_name=?,weight_kg=?,m1=?,m2=?,m3=?,m4=?,m5=?,
                    l1=?,l2=?,l3=?,l4=?,l5=?,active=? WHERE id=?""",
                 ((f.get("name") or "").strip(), (f.get("display_name") or "").strip(), _flt(f.get("weight_kg")),
                  _int(f.get("m1")), _int(f.get("m2")), _int(f.get("m3")), _int(f.get("m4")), _int(f.get("m5")),
                  _int(f.get("l1")), _int(f.get("l2")), _int(f.get("l3")), _int(f.get("l4")), _int(f.get("l5")),
                  1 if f.get("active") else 0, pid))
    conn.commit()
    conn.close()
    flash("Artikel bijgewerkt.")
    return redirect(url_for("planning.products"))


@bp.route("/products/<int:pid>/delete", methods=["POST"])
def product_delete(pid):
    guard = login_required("manage_settings")
    if guard:
        return guard
    conn = db()
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    flash("Artikel verwijderd.")
    return redirect(url_for("planning.products"))


@bp.route("/orders/<int:oid>/maatwerk", methods=["GET", "POST"])
def order_maatwerk(oid):
    guard = login_required("edit_planning")
    if guard:
        return guard
    conn = db()
    o = conn.execute("""SELECT o.*, c.name AS client FROM orders o
                        LEFT JOIN clients c ON c.id=o.client_id WHERE o.id=?""", (oid,)).fetchone()
    if not o:
        conn.close()
        abort(404)
    prods = _load_products(conn)
    items = conn.execute("SELECT id,name,qty,montage_custom FROM order_items WHERE order_id=? ORDER BY id",
                         (oid,)).fetchall()
    if request.method == "POST":
        for it in items:
            if not _match_product(it["name"], prods):
                val = request.form.get("m_%d" % it["id"])
                mv = _int(val, 0) if (val is not None and val.strip() != "") else None
                conn.execute("UPDATE order_items SET montage_custom=? WHERE id=?", (mv, it["id"]))
        conn.commit()
        conn.close()
        flash("Maatwerk-tijden opgeslagen.")
        return redirect(request.args.get("next") or url_for("planning.order_detail", oid=oid))
    lev = (o["service_type"] or "montage") != "montage"
    custom, known, known_total = [], [], 0
    for it in items:
        p = _match_product(it["name"], prods)
        if p:
            tiers = p.get("tiers_lev") if lev else p["tiers"]
            if not tiers or not any(tiers):
                tiers = p["tiers"]
            t = _montage_for_qty(tiers, int(it["qty"] or 1))
            known.append({"name": it["name"], "qty": it["qty"], "min": t})
            known_total += t
        else:
            custom.append(it)
    conn.close()
    return render_template("planning/maatwerk.html", o=o, custom=custom, known=known,
                           known_total=known_total, nxt=request.args.get("next"))


def _shopify_import_order(o):
    """Maak van een Shopify-orderpayload een 'in te plannen' order met alle regels
    (ook handmatig toegevoegde/custom producten). Idempotent op shopify_id."""
    sid = str(o.get("id") or "")
    gid = ("gid://shopify/Order/%s" % sid) if sid else None
    conn = db()
    if gid and conn.execute("SELECT id FROM orders WHERE shopify_id=?", (gid,)).fetchone():
        conn.close()
        return "dup"
    name = o.get("name") or ("#" + str(o.get("order_number") or ""))
    onum = str(o.get("order_number") or name.lstrip("#") or sid)
    cust = o.get("customer") or {}
    ship = o.get("shipping_address") or o.get("billing_address") or {}
    client_name = (ship.get("company")
                   or ("%s %s" % (cust.get("first_name", ""), cust.get("last_name", ""))).strip()
                   or ship.get("name") or "Shopify-klant")
    email = (o.get("email") or cust.get("email") or "").strip()
    phone = (ship.get("phone") or o.get("phone") or "").strip()
    address1 = (ship.get("address1") or "").strip()
    city = (ship.get("city") or "").strip()
    postal = (ship.get("zip") or "").strip()
    full = ", ".join([p for p in [address1, (postal + " " + city).strip()] if p])
    amount = float(o.get("total_price") or 0)
    note = (o.get("note") or "").strip()
    # Montage (M) of levering (L) afleiden uit de Shopify-verzendmethode/artikelen/notitie.
    # Shopify-teksten: "Delivery including installation ..." = montage,
    #                  "Delivery without installation ..."  = levering.
    _blob = (" ".join((li.get("title") or li.get("name") or "") for li in (o.get("line_items") or []))
             + " " + " ".join((sl.get("title") or sl.get("code") or "") for sl in (o.get("shipping_lines") or []))
             + " " + note).lower()
    if "without installation" in _blob or "zonder montage" in _blob or "no installation" in _blob:
        service = "levering"
    elif "installation" in _blob or "montage" in _blob:
        service = "montage"
    else:
        service = "levering"
    cl = None
    if email:
        cl = conn.execute("SELECT id FROM clients WHERE lower(email)=?", (email.lower(),)).fetchone()
    if not cl:
        cl = conn.execute("SELECT id FROM clients WHERE name=?", (client_name,)).fetchone()
    if cl:
        cid = cl["id"]
    else:
        conn.execute("INSERT INTO clients(name,email,phone,address,postal,city,created_at) VALUES(?,?,?,?,?,?,?)",
                     (client_name, email, phone, address1, postal, city, _today_iso()))
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("""INSERT INTO orders(order_number,client_id,source,is_draft,status,delivery_address,city,postal,
                    invoice_address,phone,email,amount,notes,service_type,shopify_id,track_token,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (onum, cid, "shopify", 0, "in_te_plannen", full, city, postal, full, phone, email,
                  amount, note, service, gid, _new_track_token(), _today_iso()))
    oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for li in (o.get("line_items") or []):
        qty = int(li.get("quantity") or 1)
        conn.execute("INSERT INTO order_items(order_id,name,qty) VALUES(?,?,?)", (oid, _clean_item_name(li), qty))
    # Montagetijd (workload) uit de artikelcatalogus afleiden.
    prods = _load_products(conn)
    itr = conn.execute("SELECT name,qty,montage_custom FROM order_items WHERE order_id=?", (oid,)).fetchall()
    mm = _order_montage([{"name": r["name"], "qty": r["qty"], "montage_custom": r["montage_custom"]} for r in itr], prods, fallback=0, service_type=service)
    if mm:
        conn.execute("UPDATE orders SET montage_min=? WHERE id=?", (mm, oid))
    conn.commit()
    conn.close()
    return "ok"


@bp.route("/api/shopify/webhook", methods=["POST"])
def shopify_webhook():
    raw = request.get_data()
    secret = (_shopify_cfg().get("webhook_secret") or "").strip()
    if not secret:
        return ("Shopify-koppeling niet ingesteld (webhook-secret ontbreekt).", 503)
    sent = request.headers.get("X-Shopify-Hmac-Sha256", "")
    digest = base64.b64encode(hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()).decode()
    if not (sent and hmac.compare_digest(digest, sent)):
        return ("Ongeldige handtekening.", 401)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return ("Ongeldige payload.", 400)
    try:
        _shopify_import_order(payload)
    except Exception as e:
        # 500 → Shopify probeert het later opnieuw (geen orders kwijt)
        return ("Importfout: %s" % e, 500)
    return ("ok", 200)


@bp.route("/free-days", methods=["GET", "POST"])
def free_days():
    guard = login_required("manage_freedays")
    if guard:
        return guard
    u = current_user()
    conn = db()
    if request.method == "POST":
        conn.execute("""INSERT INTO free_days(monteur_id,type,date_from,date_to,note) VALUES(?,?,?,?,?)""",
                     (request.form.get("monteur_id"), request.form.get("type"),
                      request.form.get("date_from"),
                      request.form.get("date_to") or request.form.get("date_from"),
                      request.form.get("note", "")))
        conn.commit()
        flash("Vrije dag geregistreerd.")
    rows = conn.execute("""SELECT f.*, m.name AS monteur FROM free_days f
                           LEFT JOIN monteurs m ON m.id=f.monteur_id ORDER BY f.date_from DESC""").fetchall()
    monteurs = conn.execute("SELECT * FROM monteurs WHERE active=1 ORDER BY name").fetchall()
    # aanvragen die ik mag goedkeuren
    appr = []
    if _email(u) in APPROVERS_MONTEUR:
        appr.append("is_monteur=1")
    if _email(u) in APPROVERS_OFFICE:
        appr.append("is_monteur=0")
    open_reqs = []
    if appr:
        open_reqs = conn.execute("SELECT * FROM leave_requests WHERE status='open' AND (%s) ORDER BY created_at"
                                 % " OR ".join(appr)).fetchall()
    history = conn.execute("SELECT * FROM leave_requests WHERE status!='open' ORDER BY decided_at DESC LIMIT 50").fetchall()
    my_reqs = conn.execute("SELECT * FROM leave_requests WHERE user_id=? ORDER BY created_at DESC LIMIT 20", (u["id"],)).fetchall()
    conn.close()
    # tot wie kan deze gebruiker een aanvraag richten?
    approvers_label = "Aleks, Stijn, Jorik en Caspar" if u["role"] == "monteur" else "Aleks, Caspar en Jorik"
    return render_template("planning/free_days.html", rows=rows, monteurs=monteurs,
                           open_reqs=open_reqs, history=history, my_reqs=my_reqs,
                           is_approver=bool(appr), approvers_label=approvers_label)


@bp.route("/api/leave-request", methods=["POST"])
def api_leave_request():
    u = current_user()
    if not u:
        return jsonify(ok=False), 403
    f = request.form
    cat = f.get("category", "verlof")
    conn = db()
    conn.execute("""INSERT INTO leave_requests(user_id,user_name,is_monteur,monteur_id,category,leave_type,
                    date_from,date_to,time_from,time_to,reason,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
                 (u["id"], u["name"], 1 if u["role"] == "monteur" else 0, u["monteur_id"],
                  cat, (f.get("leave_type") or "afspraak" if cat == "afspraak" else f.get("leave_type", "vrij")),
                  f.get("date_from"), f.get("date_to") or f.get("date_from"),
                  f.get("time_from"), f.get("time_to"), f.get("reason", ""),
                  datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/leave-decide/<int:rid>", methods=["POST"])
def api_leave_decide(rid):
    u = current_user()
    if not u:
        return jsonify(ok=False), 403
    conn = db()
    r = conn.execute("SELECT * FROM leave_requests WHERE id=?", (rid,)).fetchone()
    if not r:
        conn.close()
        return jsonify(ok=False, error="Niet gevonden"), 404
    if not can_approve(u, r["is_monteur"]):
        conn.close()
        return jsonify(ok=False, error="Geen rechten om dit goed te keuren"), 403
    data = request.get_json(force=True)
    decision = "goedgekeurd" if data.get("approve") else "afgewezen"
    reason = (data.get("reason") or "").strip()
    conn.execute("""UPDATE leave_requests SET status=?, decided_by=?, decision_reason=?, decided_at=?, decided_seen=0
                    WHERE id=?""", (decision, u["name"], reason, datetime.now().isoformat(timespec="minutes"), rid))
    if decision == "goedgekeurd":
        if r["category"] == "verlof" and r["is_monteur"] and r["monteur_id"]:
            conn.execute("""INSERT INTO free_days(monteur_id,type,date_from,date_to,note) VALUES(?,?,?,?,?)""",
                         (r["monteur_id"], r["leave_type"] or "vrij", r["date_from"], r["date_to"],
                          "Aanvraag goedgekeurd" + ((" · " + r["reason"]) if r["reason"] else "")))
        elif not r["is_monteur"]:
            # kantoorgebruiker: in de kantoorbezetting zetten (per dag)
            try:
                d0 = datetime.strptime(r["date_from"], "%Y-%m-%d").date()
                d1 = datetime.strptime(r["date_to"] or r["date_from"], "%Y-%m-%d").date()
                note = ("Afspraak " + (r["time_from"] or "") + "-" + (r["time_to"] or "")) if r["category"] == "afspraak" else "Verlof (goedgekeurd)"
                cur = d0
                while cur <= d1:
                    conn.execute("""INSERT INTO office_days(person,date,status,note) VALUES(?,?,?,?)
                                    ON CONFLICT(person,date) DO UPDATE SET status=excluded.status,note=excluded.note""",
                                 (r["user_name"], cur.isoformat(),
                                  "afspraak" if r["category"] == "afspraak" else "vrij", note))
                    cur += timedelta(days=1)
            except Exception:
                pass
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/leave-seen", methods=["POST"])
def api_leave_seen():
    u = current_user()
    if not u:
        return jsonify(ok=False), 403
    conn = db()
    conn.execute("UPDATE leave_requests SET decided_seen=1 WHERE user_id=? AND status!='open'", (u["id"],))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
#  Routes (start = thuisadres monteur, eind = Breda)
# --------------------------------------------------------------------------- #
@bp.route("/routes")
def routes():
    guard = login_required("edit_routes")
    if guard:
        return guard
    day = request.args.get("day", _today_iso())
    conn = db()
    monteurs = conn.execute("SELECT * FROM monteurs WHERE active=1 ORDER BY id").fetchall()
    jobs = conn.execute("""SELECT p.*, o.order_number, o.delivery_address, o.montage_min,
                           c.name AS client, c.city FROM planning p
                           JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
                           WHERE p.date=? ORDER BY p.monteur_id, p.sequence""", (day,)).fetchall()
    conn.close()
    routes_by_m = {}
    for j in jobs:
        routes_by_m.setdefault(j["monteur_id"], []).append(j)
    return render_template("planning/routes.html", monteurs=monteurs, routes=routes_by_m, day=day,
                           maps_ready=(integ_status("google_maps") == "verbonden"),
                           route_ready=(integ_status("route_api") == "verbonden"))


# --------------------------------------------------------------------------- #
#  Kilometerregistratie per voertuig
# --------------------------------------------------------------------------- #
@bp.route("/kilometers")
def vehicle_km():
    guard = login_required("view_reports")
    if guard:
        return guard
    today = datetime.now().date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    month_start = today.replace(day=1).isoformat()
    today_iso = today.isoformat()
    conn = db()
    busses = conn.execute("SELECT * FROM busses ORDER BY name").fetchall()
    rows = []
    for b in busses:
        def s(since):
            return conn.execute("SELECT COALESCE(SUM(km),0) FROM vehicle_km WHERE bus_id=? AND date>=?",
                                (b["id"], since)).fetchone()[0]
        dag = conn.execute("SELECT COALESCE(SUM(km),0) FROM vehicle_km WHERE bus_id=? AND date=?",
                           (b["id"], today_iso)).fetchone()[0]
        rows.append({"bus": b, "dag": round(dag), "week": round(s(week_start)), "maand": round(s(month_start))})
    # laatste 10 werkdagen voor de tabel
    recent = conn.execute("""SELECT date FROM vehicle_km GROUP BY date ORDER BY date DESC LIMIT 10""").fetchall()
    recent_dates = [r["date"] for r in recent]
    daily = {}
    for r in conn.execute("SELECT bus_id,date,km FROM vehicle_km WHERE date>=?",
                          (recent_dates[-1] if recent_dates else today_iso,)).fetchall():
        daily[(r["bus_id"], r["date"])] = round(r["km"])
    conn.close()
    return render_template("planning/vehicle_km.html", rows=rows, busses=busses,
                           recent_dates=recent_dates, daily=daily,
                           totals_day=sum(r["dag"] for r in rows),
                           totals_week=sum(r["week"] for r in rows),
                           totals_month=sum(r["maand"] for r in rows))


# --------------------------------------------------------------------------- #
#  Koppelingen
# --------------------------------------------------------------------------- #
@bp.route("/integrations", methods=["GET", "POST"])
def integrations():
    guard = login_required("manage_integrations")
    if guard:
        return guard
    conn = db()
    if request.method == "POST":
        ikey = request.form.get("ikey")
        integ = INTEGRATION_BY_KEY.get(ikey)
        if integ:
            for f in integ["fields"]:
                if f.get("lock_off"):
                    val = "0"
                elif f["type"] == "toggle":
                    val = "1" if request.form.get(f["key"]) else "0"
                else:
                    val = request.form.get(f["key"], "")
                conn.execute("""INSERT INTO integrations(ikey,field,value) VALUES(?,?,?)
                                ON CONFLICT(ikey,field) DO UPDATE SET value=excluded.value""",
                             (ikey, f["key"], val))
            conn.commit()
            flash(f"Koppeling '{integ['name']}' opgeslagen.")
        conn.close()
        return redirect(url_for("planning.integrations"))
    values = {}
    for r in conn.execute("SELECT ikey,field,value FROM integrations").fetchall():
        values.setdefault(r["ikey"], {})[r["field"]] = r["value"]
    conn.close()
    statuses = {i["key"]: integ_status(i["key"]) for i in INTEGRATIONS}
    return render_template("planning/integrations.html", integrations=INTEGRATIONS,
                           values=values, statuses=statuses)


@bp.route("/integrations/test/<ikey>", methods=["POST"])
def integration_test(ikey):
    if not has_perm("manage_integrations"):
        return jsonify(ok=False, error="Geen rechten"), 403
    if ikey not in INTEGRATION_BY_KEY:
        return jsonify(ok=False, error="Onbekende koppeling"), 404
    if ikey == "email":
        c = _email_cfg()
        from_email = (c.get("from_email") or c.get("smtp_user") or "").strip()
        if not from_email:
            return jsonify(ok=False, message="Vul eerst de afzender-e-mail in.")
        u = current_user()
        test_to = (u["email"] if u and u["email"] else from_email)
        frm = "%s <%s>" % ((c.get("from_name") or "Office-Interior").strip(), from_email)
        reply_to = (c.get("reply_to") or "").strip()
        text = "Dit is een testmail van OfficeRoute. De mailkoppeling werkt."
        html = _brand_email("Testmail geslaagd",
                            ["Dit is een testbericht van OfficeRoute.",
                             "De koppeling werkt — klantmails worden verstuurd zodra 'E-mails écht versturen' aanstaat."])
        key = (c.get("resend_api_key") or "").strip()
        if key:
            try:
                _body = {"from": frm, "to": [test_to], "subject": "OfficeRoute — testmail", "text": text, "html": html}
                if reply_to:
                    _body["reply_to"] = reply_to
                payload = json.dumps(_body).encode("utf-8")
                req = urllib.request.Request("https://api.resend.com/emails", data=payload,
                                             headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
                return jsonify(ok=True, message="Testmail verstuurd naar %s via Resend — controleer de inbox." % test_to)
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode()[:300]
                except Exception:
                    detail = e.reason
                return jsonify(ok=False, message="Resend weigerde (code %s): %s" % (e.code, detail))
            except Exception as e:
                return jsonify(ok=False, message="Versturen via Resend mislukt: %s" % e)
        host = (c.get("smtp_host") or "").strip()
        user = (c.get("smtp_user") or "").strip()
        pwd = (c.get("smtp_pass") or "").strip()
        if not (host and user and pwd):
            return jsonify(ok=False, message="Vul een Resend API-sleutel in (SMTP wordt door Render geblokkeerd).")
        try:
            msg = EmailMessage()
            msg["Subject"] = "OfficeRoute — testmail"
            msg["From"] = frm
            msg["To"] = test_to
            msg.set_content(text)
            msg.add_alternative(html, subtype="html")
            with smtplib.SMTP(host, int(c.get("smtp_port") or 587), timeout=15) as s:
                s.starttls()
                s.login(user, pwd)
                s.send_message(msg)
            return jsonify(ok=True, message="Testmail verstuurd via SMTP naar %s." % test_to)
        except Exception as e:
            return jsonify(ok=False, message="Versturen mislukt: %s" % e)
    if ikey == "shopify":
        sh = _shopify_cfg()
        if not (sh.get("webhook_secret") or "").strip():
            return jsonify(ok=False, message="Vul het Webhook-secret in (verplicht). Shop-URL is aanbevolen; "
                                             "de API-velden zijn optioneel (alleen nodig voor backfill van oude orders).")
        return jsonify(ok=True, message="Shopify-webhook staat klaar. Plaats een testorder in Shopify "
                                        "(of klik 'Testmelding versturen') — de order verschijnt bij 'Nog in te plannen'.")
    if integ_status(ikey) == "verbonden":
        return jsonify(ok=True, message="Verbinding gereed. De API-logica kan nu worden ingeschakeld.")
    return jsonify(ok=False, message="Vul eerst alle verplichte velden in om de koppeling klaar te zetten.")


# --------------------------------------------------------------------------- #
#  Gebruikers / rollen / rechten
# --------------------------------------------------------------------------- #
@bp.route("/users")
def users():
    guard = login_required("manage_users")
    if guard:
        return guard
    conn = db()
    rows = conn.execute("SELECT * FROM users ORDER BY role, name").fetchall()
    conn.close()
    parsed = []
    for u in rows:
        d = dict(u)
        try:
            d["perm_count"] = len(json.loads(u["permissions"] or "[]"))
        except Exception:
            d["perm_count"] = 0
        parsed.append(d)
    return render_template("planning/users.html", users=parsed)


@bp.route("/users/new", methods=["GET", "POST"])
def user_new():
    guard = login_required("manage_users")
    if guard:
        return guard
    conn = db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        role = request.form.get("role") or "monteur"
        pw = (request.form.get("new_password") or "").strip()
        mid = request.form.get("monteur_id") or None
        if mid in ("", "0", None):
            mid = None
        active = 1 if request.form.get("active") else 0
        if not (name and email and pw):
            conn.close(); flash("Naam, e-mail en wachtwoord zijn verplicht.")
            return redirect(url_for("planning.user_new"))
        if conn.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone():
            conn.close(); flash("Dat e-mailadres bestaat al.")
            return redirect(url_for("planning.user_new"))
        perms = list(ROLE_DEFAULTS.get(role, []))
        conn.execute("""INSERT INTO users(name,email,password,role,permissions,monteur_id,active,created_at)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (name, email, _hash_pw(pw), role, json.dumps(perms), mid, active, _today_iso()))
        conn.commit(); conn.close()
        flash("Gebruiker aangemaakt.")
        return redirect(url_for("planning.users"))
    monteurs = conn.execute("SELECT id,name FROM monteurs WHERE active=1 ORDER BY name").fetchall()
    conn.close()
    return render_template("planning/user_new.html", roles=ROLE_LABELS, monteurs=monteurs)


@bp.route("/users/<int:uid>", methods=["GET", "POST"])
def user_edit(uid):
    guard = login_required("manage_roles")
    if guard:
        return guard
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        conn.close(); abort(404)
    if request.method == "POST":
        name = (request.form.get("name") or u["name"]).strip()
        email = (request.form.get("email") or u["email"]).strip().lower()
        role = request.form.get("role", u["role"])
        perms = [k for k in PERMISSION_KEYS if request.form.get("perm_" + k)]
        active = 1 if request.form.get("active") else 0
        mid = request.form.get("monteur_id") or None
        if mid in ("", "0", None):
            mid = None
        if conn.execute("SELECT id FROM users WHERE lower(email)=? AND id!=?", (email, uid)).fetchone():
            conn.close(); flash("Dat e-mailadres is al bij een andere gebruiker in gebruik.")
            return redirect(url_for("planning.user_edit", uid=uid))
        conn.execute("UPDATE users SET name=?, email=?, role=?, permissions=?, active=?, monteur_id=? WHERE id=?",
                     (name, email, role, json.dumps(perms), active, mid, uid))
        newpw = (request.form.get("new_password") or "").strip()
        if newpw:
            conn.execute("UPDATE users SET password=? WHERE id=?", (_hash_pw(newpw), uid))
        conn.commit(); conn.close()
        flash("Gebruiker bijgewerkt.")
        return redirect(url_for("planning.users"))
    try:
        current = set(json.loads(u["permissions"] or "[]"))
    except Exception:
        current = set()
    monteurs = conn.execute("SELECT id,name FROM monteurs WHERE active=1 ORDER BY name").fetchall()
    conn.close()
    groups = {}
    for k, label, grp in PERMISSIONS:
        groups.setdefault(grp, []).append((k, label))
    return render_template("planning/user_edit.html", u=u, groups=groups, current=current,
                           roles=ROLE_LABELS, role_defaults=ROLE_DEFAULTS, monteurs=monteurs)


# --------------------------------------------------------------------------- #
#  Bedrijfsinstellingen + e-mailtemplates
# --------------------------------------------------------------------------- #
@bp.route("/settings", methods=["GET", "POST"])
def company_settings():
    guard = login_required("manage_settings")
    if guard:
        return guard
    conn = db()
    if request.method == "POST":
        for k in ("company_name", "home_base", "tpl_confirm", "tpl_arrival", "tpl_delay"):
            conn.execute("""INSERT INTO settings(skey,value) VALUES(?,?)
                            ON CONFLICT(skey) DO UPDATE SET value=excluded.value""",
                         (k, request.form.get(k, "")))
        conn.commit()
        flash("Instellingen opgeslagen.")
    vals = {r["skey"]: r["value"] for r in conn.execute("SELECT skey,value FROM settings").fetchall()}
    conn.close()
    return render_template("planning/settings.html", v=vals)


# --------------------------------------------------------------------------- #
#  Monteur-app (mobiel) + live GPS delen
# --------------------------------------------------------------------------- #
@bp.route("/monteur")
def monteur_app():
    guard = login_required("monteur_app")
    if guard:
        return guard
    u = current_user()
    conn = db()
    mid = u["monteur_id"]
    today = _today_iso()
    jobs, monteur = [], None
    if mid:
        monteur = conn.execute("SELECT * FROM monteurs WHERE id=?", (mid,)).fetchone()
        jobs = conn.execute("""SELECT p.*, o.id AS oid, o.order_number, o.delivery_address, o.phone, o.instructions,
                               o.montage_min, o.service_type, o.pakbon, c.name AS client,
                               (SELECT GROUP_CONCAT(qty || 'x ' || name, ', ') FROM order_items WHERE order_id=o.id) AS items
                               FROM planning p JOIN orders o ON o.id=p.order_id
                               LEFT JOIN clients c ON c.id=o.client_id
                               WHERE p.monteur_id=? AND p.date=? ORDER BY p.sequence""", (mid, today)).fetchall()
    closed = False
    if mid:
        closed = conn.execute("SELECT 1 FROM route_closed WHERE monteur_id=? AND date=?",
                              (mid, today)).fetchone() is not None
    conn.close()
    alerts = route_alerts(mid, bool(jobs)) if mid else []
    all_done = bool(jobs) and all(j["status"] == "afgerond" for j in jobs)
    return render_template("planning/monteur_app.html", monteur=monteur, jobs=jobs, alerts=alerts,
                           closed=closed, all_done=all_done,
                           maps_ready=(integ_status("google_maps") == "verbonden"))


def cleanup_old_signatures():
    """Handtekeningen ouder dan 30 dagen automatisch verwijderen (privacy)."""
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    try:
        conn = db()
        conn.execute("DELETE FROM deliveries WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception:
        pass


@bp.route("/monteur/complete/<int:pid>", methods=["POST"])
def monteur_complete(pid):
    if not has_perm("monteur_app") and not has_perm("complete_deliveries"):
        abort(403)
    receiver = (request.form.get("receiver") or "").strip()
    signature = request.form.get("signature") or ""
    outcome = request.form.get("outcome") or "succesvol"
    sub = request.form.get("sub_outcome") or ""
    # Handtekening + ontvanger verplicht bij een succesvolle levering.
    if outcome == "succesvol" and (not receiver or not signature):
        return jsonify(ok=False, error="Ontvanger en handtekening zijn verplicht."), 400
    conn = db()
    p = conn.execute("SELECT * FROM planning WHERE id=?", (pid,)).fetchone()
    if p:
        conn.execute("UPDATE planning SET status='afgerond' WHERE id=?", (pid,))
        # Order afronden én automatisch afhandelen naar Shopify (klaar voor facturatie/betaling).
        conn.execute("UPDATE orders SET status='afgerond', fulfilled=1, fulfilled_at=? WHERE id=?",
                     (datetime.now().isoformat(timespec="minutes"), p["order_id"]))
        conn.execute("""INSERT INTO deliveries(order_id,monteur_id,receiver,signature,outcome,sub_outcome,ts)
                        VALUES(?,?,?,?,?,?,?)""",
                     (p["order_id"], p["monteur_id"], receiver, signature, outcome, sub,
                      datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    conn.close()
    cleanup_old_signatures()
    return jsonify(ok=True)


@bp.route("/app")
def app_home():
    """Deelbare mobiele link voor monteurs."""
    u = current_user()
    if not u:
        return redirect(url_for("planning.login", next=url_for("planning.monteur_app")))
    if u["role"] == "monteur" or u["monteur_id"]:
        return redirect(url_for("planning.monteur_app"))
    return redirect(url_for("planning.dashboard"))


@bp.route("/monteur/start/<int:pid>", methods=["POST"])
def monteur_start(pid):
    if not has_perm("monteur_app"):
        return jsonify(ok=False), 403
    u = current_user()
    conn = db()
    p = conn.execute("SELECT * FROM planning WHERE id=? AND monteur_id=?", (pid, u["monteur_id"])).fetchone()
    if p:
        conn.execute("UPDATE planning SET status='onderweg' WHERE id=?", (pid,))
        conn.execute("UPDATE orders SET status='onderweg' WHERE id=?", (p["order_id"],))
        conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/monteur/close-route", methods=["POST"])
def close_route():
    """Monteur sluit zijn route voor vandaag; klantgegevens verdwijnen uit zijn app (privacy)."""
    u = current_user()
    if not u or not u["monteur_id"]:
        return jsonify(ok=False), 403
    conn = db()
    conn.execute("""INSERT INTO route_closed(monteur_id,date,ts) VALUES(?,?,?)
                    ON CONFLICT(monteur_id,date) DO UPDATE SET ts=excluded.ts""",
                 (u["monteur_id"], _today_iso(), datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/route-geo/<int:mid>")
def api_route_geo(mid):
    """Coördinaten voor 'Bekijk route' op de kaart (home -> stops -> Breda)."""
    if not current_user():
        return jsonify(ok=False), 403
    day = request.args.get("day", _today_iso())
    conn = db()
    m = conn.execute("SELECT name,home_lat,home_lng,home_address FROM monteurs WHERE id=?", (mid,)).fetchone()
    rows = conn.execute("""SELECT o.city, o.delivery_address, c.name AS client FROM planning p
                           JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
                           WHERE p.monteur_id=? AND p.date=? ORDER BY p.sequence""", (mid, day)).fetchall()
    conn.close()
    pts = []
    if m and m["home_lat"]:
        pts.append({"lat": m["home_lat"], "lng": m["home_lng"], "label": "Start: " + (m["home_address"] or "thuis")})
    for r in rows:
        c = CITY_COORDS.get(r["city"])
        if c:
            pts.append({"lat": c[0], "lng": c[1], "label": (r["client"] or "") + " · " + (r["city"] or ""),
                        "address": r["delivery_address"]})
    pts.append({"lat": BREDA[0], "lng": BREDA[1], "label": "Terug: " + HOME_BASE})
    return jsonify(ok=True, name=(m["name"] if m else ""), points=pts)


OUTCOMES = ["succesvol", "beschadigd", "niet_thuis"]
OUTCOME_LABELS = {"succesvol": "Succesvol", "beschadigd": "Beschadigd/incompleet", "niet_thuis": "Niet thuis"}


def _performance_data():
    today = datetime.now().date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    month_start = today.replace(day=1).isoformat()
    conn = db()
    monteurs = conn.execute("SELECT id,name FROM monteurs ORDER BY name").fetchall()
    report = []
    tot_wk = {oc: 0 for oc in OUTCOMES}
    tot_mo = {oc: 0 for oc in OUTCOMES}
    for m in monteurs:
        def cnt(since, oc):
            return conn.execute("SELECT COUNT(*) FROM deliveries WHERE monteur_id=? AND ts>=? AND outcome=?",
                                (m["id"], since, oc)).fetchone()[0]
        wk = {oc: cnt(week_start, oc) for oc in OUTCOMES}
        mo = {oc: cnt(month_start, oc) for oc in OUTCOMES}
        # 'adres in 1x correct'-ratio (kwaliteit)
        ok1 = conn.execute("""SELECT COUNT(*) FROM deliveries WHERE monteur_id=? AND ts>=? AND sub_outcome=?""",
                           (m["id"], month_start, "adres in 1x correct")).fetchone()[0]
        succ_mo = mo["succesvol"] or 0
        if sum(wk.values()) + sum(mo.values()) > 0:
            for oc in OUTCOMES:
                tot_wk[oc] += wk[oc]; tot_mo[oc] += mo[oc]
            report.append({"name": m["name"], "week": wk, "month": mo,
                           "wk_total": sum(wk.values()), "mo_total": sum(mo.values()),
                           "addr_ok_pct": (round(ok1 / succ_mo * 100) if succ_mo else None)})
    conn.close()
    return report, tot_wk, tot_mo


@bp.route("/rapportages/prestaties")
def performance():
    guard = login_required("view_performance")
    if guard:
        return guard
    report, tot_wk, tot_mo = _performance_data()
    return render_template("planning/performance.html", report=report, tot_wk=tot_wk, tot_mo=tot_mo,
                           labels=OUTCOME_LABELS)


@bp.route("/rapportages/prestaties/export.csv")
def performance_export():
    guard = login_required("view_performance")
    if guard:
        return guard
    report, _, _ = _performance_data()
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Monteur", "Week succesvol", "Week beschadigd", "Week niet thuis",
                "Maand succesvol", "Maand beschadigd", "Maand niet thuis", "Adres-1x-correct %"])
    for r in report:
        w.writerow([r["name"], r["week"]["succesvol"], r["week"]["beschadigd"], r["week"]["niet_thuis"],
                    r["month"]["succesvol"], r["month"]["beschadigd"], r["month"]["niet_thuis"],
                    (r["addr_ok_pct"] if r["addr_ok_pct"] is not None else "")])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=monteursprestaties.csv"})


@bp.route("/rapportages/handtekeningen")
def signatures():
    guard = login_required("view_signatures")
    if guard:
        return guard
    cleanup_old_signatures()
    q = (request.args.get("q") or "").strip().lstrip("#")
    conn = db()
    sql = """SELECT d.*, o.order_number, c.name AS client, m.name AS monteur
             FROM deliveries d LEFT JOIN orders o ON o.id=d.order_id
             LEFT JOIN clients c ON c.id=o.client_id LEFT JOIN monteurs m ON m.id=d.monteur_id"""
    args = ()
    if q:
        like = "%" + q + "%"
        sql += """ WHERE o.order_number LIKE ? OR c.name LIKE ? OR d.receiver LIKE ? OR m.name LIKE ?"""
        args = (like, like, like, like)
    sql += " ORDER BY d.ts DESC LIMIT 500"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    by_date = {}
    for r in rows:
        d = (r["ts"] or "")[:10]
        by_date.setdefault(d, []).append(r)
    days = sorted(by_date.keys(), reverse=True)
    return render_template("planning/signatures.html", by_date=by_date, days=days, q=q)


@bp.route("/kilometers/export.csv")
def vehicle_km_export():
    guard = login_required("view_reports")
    if guard:
        return guard
    today = datetime.now().date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    month_start = today.replace(day=1).isoformat()
    conn = db()
    busses = conn.execute("SELECT * FROM busses ORDER BY name").fetchall()
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Voertuig", "Kenteken", "Vandaag (km)", "Deze week (km)", "Deze maand (km)"])
    for b in busses:
        dag = conn.execute("SELECT COALESCE(SUM(km),0) FROM vehicle_km WHERE bus_id=? AND date=?", (b["id"], today.isoformat())).fetchone()[0]
        wk = conn.execute("SELECT COALESCE(SUM(km),0) FROM vehicle_km WHERE bus_id=? AND date>=?", (b["id"], week_start)).fetchone()[0]
        mo = conn.execute("SELECT COALESCE(SUM(km),0) FROM vehicle_km WHERE bus_id=? AND date>=?", (b["id"], month_start)).fetchone()[0]
        w.writerow([b["name"], b["plate"], round(dag), round(wk), round(mo)])
    conn.close()
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=kilometers.csv"})


@bp.route("/api/optimize-route/<int:mid>", methods=["POST"])
def api_optimize_route(mid):
    """Optimaliseer de stopvolgorde binnen een route (nearest-neighbor vanaf thuisadres)."""
    if not has_perm("optimize_routes") and not has_perm("edit_planning"):
        return jsonify(ok=False, error="Geen rechten"), 403
    data = request.get_json(silent=True) or {}
    day = data.get("day") or _today_iso()
    conn = db()
    m = conn.execute("SELECT home_lat,home_lng,speed FROM monteurs WHERE id=?", (mid,)).fetchone()
    rows = conn.execute("""SELECT p.id pid, o.city, o.montage_min FROM planning p JOIN orders o ON o.id=p.order_id
                           WHERE p.monteur_id=? AND p.date=? ORDER BY p.sequence""", (mid, day)).fetchall()
    if not rows:
        conn.close()
        return jsonify(ok=True, optimized=0)
    start = (m["home_lat"], m["home_lng"]) if m and m["home_lat"] else BREDA
    speed = (m["speed"] if m else 3) or 3
    mfac = 1.0 + (3 - speed) * 0.08
    remaining = [{"pid": r["pid"], "coord": CITY_COORDS.get(r["city"], BREDA), "montage": r["montage_min"] or 0} for r in rows]
    order, cur = [], start
    while remaining:
        nxt = min(remaining, key=lambda x: haversine(cur, x["coord"]))
        order.append(nxt); remaining.remove(nxt); cur = nxt["coord"]
    # nieuwe volgorde + geplande tijden vanaf 08:00
    t = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    pos = start
    for i, it in enumerate(order):
        t = t + timedelta(minutes=haversine(pos, it["coord"]) / 45 * 60)
        st = t.strftime("%H:%M")
        t2 = t + timedelta(minutes=it["montage"] * mfac)
        conn.execute("UPDATE planning SET sequence=?, slot_start=?, slot_end=? WHERE id=?",
                     (i, st, t2.strftime("%H:%M"), it["pid"]))
        t = t2; pos = it["coord"]
    conn.commit()
    conn.close()
    return jsonify(ok=True, optimized=len(order))


def auto_send_daily_mails():
    """Automatisch: 's ochtends het tijdvak en bij vertraging (>=20 min) een update.
    Draait wanneer de planner het dashboard opent (geen externe cron op gratis plan)."""
    if not _mail_live():
        return 0   # testmodus: geen automatische mails
    today = _today_iso()
    conn = db()
    s = {r["skey"]: r["value"] for r in conn.execute("SELECT skey,value FROM settings").fetchall()}
    tpl_a = s.get("tpl_arrival", "Beste {klant}, vandaag leveren wij tussen {tijdvak}. Volg: {trackinglink}")
    tpl_d = s.get("tpl_delay", "Beste {klant}, door vertraging is de nieuwe verwachte tijd {eta}.")
    sent = 0
    # 1) aankomst-/tijdvakmails
    rows = conn.execute("""SELECT p.id pid, p.slot_start, p.slot_end, o.order_number, o.client_id, o.track_token,
                           o.email AS oemail, c.name AS client, c.email AS cemail
                           FROM planning p JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
                           WHERE p.date=? AND p.arrival_mailed=0 AND p.status!='afgerond'""", (today,)).fetchall()
    for r in rows:
        tijdvak = (r["slot_start"] or "08:00") + "–" + (r["slot_end"] or "17:00")
        link = "https://planning-o-i.onrender.com/track/%s" % r["track_token"]
        greet = "Beste %s," % (r["client"] or "klant")
        intro = _mailtxt("mailtxt_today_b")
        body = greet + "\n\n" + intro
        subject = "Uw levering vandaag #" + r["order_number"]
        html = _brand_email(_mailtxt("mailtxt_today_h"), _paras(greet, intro),
                            info=[("Bezorgdatum", _nl_date(today)), ("Tijdvak", tijdvak),
                                  ("Ordernummer", "#" + r["order_number"])],
                            button=("Volg uw levering &amp; bericht doorgeven", link))
        _send_mail((r["oemail"] or r["cemail"]), subject, body, html)
        conn.execute("""INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment) VALUES(?,?,?,?,?,0)""",
                     (r["client_id"], "out", subject, body, datetime.now().isoformat(timespec="minutes")))
        conn.execute("UPDATE planning SET arrival_mailed=1 WHERE id=?", (r["pid"],))
        sent += 1
    # 2) vertraging-updates (>= ALERT_THRESHOLD) op basis van live AT
    monteurs = conn.execute("SELECT * FROM monteurs WHERE active=1").fetchall()
    for m in monteurs:
        live = _live_loc(conn, m["id"])
        if not live:
            continue
        stops = conn.execute("""SELECT p.id pid, p.slot_start, p.delay_mailed, o.city, o.montage_min, o.status AS ostatus,
                                o.order_number, o.track_token, o.client_id, o.email AS oemail, c.name AS client, c.email AS cemail, p.status
                                FROM planning p JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
                                WHERE p.monteur_id=? AND p.date=? ORDER BY p.sequence""", (m["id"], today)).fetchall()
        arrivals, _ = compute_arrivals(stops, m, live, True)
        for st, a in zip(stops, arrivals):
            if a["status"] in ("late",) and (a["delta"] or 0) >= ALERT_THRESHOLD and not st["delay_mailed"]:
                greet = "Beste %s," % (st["client"] or "klant")
                intro = _mailtxt("mailtxt_delay_b")
                body = greet + "\n\n" + intro
                subject = "Update levertijd #" + st["order_number"]
                html = _brand_email(_mailtxt("mailtxt_delay_h"), _paras(greet, intro),
                                    info=[("Nieuwe verwachte tijd", a["at"]), ("Ordernummer", "#" + st["order_number"])],
                                    button=("Volg uw levering",
                                            "https://planning-o-i.onrender.com/track/%s" % st["track_token"]))
                _send_mail((st["oemail"] or st["cemail"]), subject, body, html)
                conn.execute("""INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment) VALUES(?,?,?,?,?,0)""",
                             (st["client_id"], "out", subject, body, datetime.now().isoformat(timespec="minutes")))
                conn.execute("UPDATE planning SET delay_mailed=1 WHERE id=?", (st["pid"],))
                sent += 1
    conn.commit()
    conn.close()
    return sent


# Idempotent initialiseren bij import (ook onder gunicorn).
init_db()
