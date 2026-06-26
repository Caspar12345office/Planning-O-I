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
    flash, jsonify, Response, abort, send_from_directory,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import sqlite3, os, json, secrets, csv, io, math, time
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
# Grens waarboven een order als "belangrijke order" geldt (euro).
IMPORTANT_THRESHOLD = 3000
# File/werkzaamheden pas tonen/notificeren vanaf deze extra vertraging (minuten).
ALERT_THRESHOLD = 20

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


def db():
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
    ("manage_freedays",    "Vrije dagen beheren",        "Personeel"),
    ("view_reports",       "Kilometers & cijfers",       "Rapportage"),
    ("export",             "Exporteren",                 "Rapportage"),
    ("view_kpis",          "KPI's & omzet inzien",       "Rapportage"),
    ("view_personnel",     "Personeelsgegevens",         "Personeel"),
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
    "manager": ["view_kpis", "view_planning", "view_reports", "view_orders",
                "view_invoices", "view_personnel", "export", "view_emails"],
    "planner": ["view_planning", "edit_planning", "plan_orders", "assign_monteurs",
                "edit_routes", "optimize_routes", "inform_clients", "manage_freedays",
                "view_reports", "view_orders", "view_personnel"],
    "administratie": ["view_orders", "edit_clients", "view_emails", "view_invoices",
                      "view_planning", "complete_deliveries"],
    "monteur": ["monteur_app"],
}
ROLE_LABELS = {"beheerder": "Beheerder", "manager": "Manager", "planner": "Planner",
               "administratie": "Administratie", "monteur": "Monteur"}


# --------------------------------------------------------------------------- #
#  Koppelingen (integraties)
# --------------------------------------------------------------------------- #
INTEGRATIONS = [
    {"key": "shopify", "name": "Shopify", "icon": "🛍",
     "desc": "Realtime import van bevestigde orders als 'Nog in te plannen'. Ordernummers komen overeen met Shopify.",
     "fields": [
        {"key": "shop_url", "label": "Shop-URL", "type": "text", "placeholder": "office-interior.myshopify.com"},
        {"key": "api_key", "label": "API-sleutel", "type": "password"},
        {"key": "api_secret", "label": "API-secret", "type": "password"},
        {"key": "access_token", "label": "Admin access token", "type": "password"},
        {"key": "webhook_secret", "label": "Webhook-secret", "type": "password"},
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
     "desc": "Automatische bevestiging, aankomst-tijdvak en trackinglink naar de klant.",
     "fields": [
        {"key": "smtp_host", "label": "SMTP-host", "type": "text", "placeholder": "smtp.office-interior.nl"},
        {"key": "smtp_user", "label": "SMTP-gebruiker", "type": "text"},
        {"key": "smtp_pass", "label": "SMTP-wachtwoord", "type": "password"},
        {"key": "from_name", "label": "Afzendernaam", "type": "text", "default": "Office-Interior Bezorging"},
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
        desired_date TEXT, notes TEXT, instructions TEXT,
        amount REAL DEFAULT 0, volume REAL DEFAULT 0, weight REAL DEFAULT 0,
        montage_min INTEGER DEFAULT 30, service_type TEXT DEFAULT 'montage',
        pakbon TEXT, shopify_id TEXT, created_at TEXT);
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
        confirmed INTEGER DEFAULT 0, mailed INTEGER DEFAULT 0, status TEXT DEFAULT 'gepland');
    CREATE TABLE IF NOT EXISTS free_days(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        monteur_id INTEGER, type TEXT, date_from TEXT, date_to TEXT, note TEXT);
    CREATE TABLE IF NOT EXISTS integrations(
        ikey TEXT, field TEXT, value TEXT, PRIMARY KEY(ikey, field));
    CREATE TABLE IF NOT EXISTS email_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER, direction TEXT, subject TEXT, body TEXT, ts TEXT, has_attachment INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS settings(skey TEXT PRIMARY KEY, value TEXT);
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
    """)
    # Defensieve migratie (bv. bestaande database met disk op Render).
    for stmt in ("ALTER TABLE users ADD COLUMN last_seen TEXT",
                 "ALTER TABLE planning ADD COLUMN mailed INTEGER DEFAULT 0",
                 "ALTER TABLE orders ADD COLUMN service_type TEXT DEFAULT 'montage'",
                 "ALTER TABLE orders ADD COLUMN pakbon TEXT",
                 "ALTER TABLE orders ADD COLUMN fulfilled INTEGER DEFAULT 0",
                 "ALTER TABLE orders ADD COLUMN fulfilled_at TEXT",
                 "ALTER TABLE monteurs ADD COLUMN standard INTEGER NOT NULL DEFAULT 1"):
        try:
            conn.execute(stmt)
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
                  (name, email, generate_password_hash("PlanningOI2025!"), role,
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
                     invoice_address,phone,email,desired_date,amount,volume,weight,montage_min,shopify_id,created_at,notes)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (num, cid, source, draft, status, full_addr, city, postal, full_addr, phone, email,
                   iso(today + timedelta(days=dft)), amount, vol, weight, montage,
                   (f"gid://shopify/Order/{1000+int(num)}" if source == "shopify" else None), iso(today), ""))
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
    uid = session.get("p_user_id")
    if not uid:
        return None
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    conn.close()
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
    required = [f["key"] for f in integ["fields"] if f["type"] in ("text", "password")]
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


@bp.app_context_processor
def _inject():
    if request.blueprint != "planning":
        return {}
    u = current_user()
    online = online_users() if u else []
    return {"p_user": u, "p_perms": user_perms(u), "p_has_perm": has_perm,
            "ROLE_LABELS": ROLE_LABELS, "HOME_BASE": HOME_BASE, "p_nav": NAV, "BRAND": BRAND,
            "p_open_questions": open_questions_count(u), "p_online": online}


def login_required(perm=None):
    u = current_user()
    if not u:
        return redirect(url_for("planning.login", next=request.path))
    if perm and perm not in user_perms(u):
        return render_template("planning/no_access.html", perm=perm), 403
    return None


# Navigatie: items met 'endpoint' (link) of 'children' (uitklapbare groep onder Instellingen/Orders).
NAV = [
    {"label": "Dashboard", "endpoint": "planning.dashboard", "icon": "▦", "perm": "view_planning"},
    {"label": "Planning & routes", "endpoint": "planning.planning", "icon": "🗓", "perm": "view_planning"},
    {"label": "Orders", "icon": "📦", "perm": "view_orders", "children": [
        {"label": "Alle orders", "endpoint": "planning.orders", "perm": "view_orders"},
        {"label": "Belangrijke orders", "endpoint": "planning.important_orders", "perm": "view_orders"}]},
    {"label": "Leveringen", "endpoint": "planning.deliveries", "icon": "✍", "perm": "view_reports"},
    {"label": "Klanten", "endpoint": "planning.clients", "icon": "👥", "perm": "view_orders"},
    {"label": "Teamchat", "endpoint": "planning.chat", "icon": "💬", "perm": "view_orders"},
    {"label": "Monteurs", "endpoint": "planning.monteurs", "icon": "🧰", "perm": "view_personnel"},
    {"label": "Bussen", "icon": "🚐", "perm": "view_personnel", "children": [
        {"label": "Bussenoverzicht", "endpoint": "planning.busses", "perm": "view_personnel"},
        {"label": "Kilometers", "endpoint": "planning.vehicle_km", "perm": "view_reports"}]},
    {"label": "Vrije dagen", "endpoint": "planning.free_days", "icon": "🏖", "perm": "manage_freedays"},
    {"label": "Instellingen", "icon": "⚙", "perm": None, "children": [
        {"label": "Bedrijfsinstellingen", "endpoint": "planning.company_settings", "perm": "manage_settings"},
        {"label": "Koppelingen", "endpoint": "planning.integrations", "perm": "manage_integrations"},
        {"label": "Gebruikers", "endpoint": "planning.users", "perm": "manage_users"}]},
]


def _today_iso():
    return datetime.now().date().isoformat()


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


def _live_loc(conn, mid):
    r = conn.execute("SELECT lat,lng,live FROM monteur_location WHERE monteur_id=?", (mid,)).fetchone()
    return (r["lat"], r["lng"]) if (r and r["live"]) else None


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
                demo_code = tf.get("code")
                twofa_email = tf.get("email")
        else:
            # Stap 1: e-mail + wachtwoord
            email = (request.form.get("email") or "").strip().lower()
            pw = request.form.get("password") or ""
            conn = db()
            u = conn.execute("SELECT * FROM users WHERE lower(email)=? AND active=1", (email,)).fetchone()
            conn.close()
            if u and check_password_hash(u["password"], pw):
                code = "%06d" % secrets.randbelow(1000000)
                session["twofa"] = {"uid": u["id"], "code": code, "exp": time.time() + 300,
                                    "next": request.args.get("next"), "email": u["email"]}
                show_2fa = True
                demo_code = code
                twofa_email = u["email"]
            else:
                error = "Onjuiste inloggegevens."
    return render_template("planning/login.html", error=error, show_2fa=show_2fa,
                           demo_code=demo_code, twofa_email=twofa_email,
                           office_accounts=_office_demo_accounts())


@bp.route("/logout")
def logout():
    session.pop("p_user_id", None)
    session.pop("twofa", None)
    return redirect(url_for("planning.login"))


# --------------------------------------------------------------------------- #
#  Dashboard
# --------------------------------------------------------------------------- #
@bp.route("/dashboard")
def dashboard():
    guard = login_required("view_planning")
    if guard:
        return guard
    u = current_user()
    conn = db()
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
        "monteurs_active": scalar("SELECT COUNT(*) FROM monteurs WHERE active=1"),
        "drafts_blocked": scalar("SELECT COUNT(*) FROM orders WHERE is_draft=1"),
        "important": scalar("SELECT COUNT(*) FROM orders WHERE amount>=? AND is_draft=0 AND desired_date>=?",
                            (IMPORTANT_THRESHOLD, today)),
    }
    # monteurs onderweg + ETA terug in Breda
    underway = []
    rows = conn.execute("""
        SELECT m.id, m.name, m.color, m.speed, l.lat, l.lng, l.updated_at
        FROM monteurs m JOIN monteur_location l ON l.monteur_id=m.id
        WHERE m.active=1 AND l.live=1""").fetchall()
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
    unplanned = unplanned_all[:4]
    monteurs = conn.execute("SELECT id,name FROM monteurs WHERE active=1 ORDER BY name").fetchall()

    # kantoorbezetting (dag selecteerbaar)
    office_day = request.args.get("office_day", today)
    office_staff = conn.execute("SELECT name FROM users WHERE role!='monteur' AND active=1 ORDER BY name").fetchall()
    od = {r["person"]: r for r in conn.execute("SELECT * FROM office_days WHERE date=?", (office_day,)).fetchall()}
    office = []
    for s in office_staff:
        rec = od.get(s["name"])
        office.append({"person": s["name"], "status": (rec["status"] if rec else "kantoor"),
                       "note": (rec["note"] if rec else "")})

    # team-vragen (@mentions) aan mij
    my_questions = conn.execute("""
        SELECT q.*, uf.name AS from_name, o.order_number FROM team_questions q
        LEFT JOIN users uf ON uf.id=q.from_user_id LEFT JOIN orders o ON o.id=q.order_id
        WHERE q.to_user_id=? AND q.resolved=0 ORDER BY q.ts DESC""", (u["id"],)).fetchall()
    all_users = conn.execute("SELECT id,name FROM users WHERE active=1 AND id!=? ORDER BY name", (u["id"],)).fetchall()
    conn.close()
    return render_template("planning/dashboard.html", stats=stats, underway=underway, unplanned=unplanned,
                           unplanned_all=unplanned_all, monteurs=monteurs,
                           office=office, office_day=office_day, today=today,
                           my_questions=my_questions, all_users=all_users)


@bp.route("/api/locations")
def api_locations():
    if not current_user():
        return jsonify([]), 403
    today = _today_iso()
    conn = db()
    rows = conn.execute("""SELECT m.id, m.name, m.color, m.speed, l.lat, l.lng, l.updated_at
                           FROM monteurs m JOIN monteur_location l ON l.monteur_id=m.id
                           WHERE m.active=1 AND l.live=1""").fetchall()
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
    if not u or not u["monteur_id"]:
        return jsonify(ok=False, error="Geen monteur"), 403
    data = request.get_json(force=True)
    lat, lng = float(data["lat"]), float(data["lng"])
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
    conn.execute("UPDATE team_questions SET resolved=1 WHERE id=?", (qid,))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("planning.dashboard"))


# --------------------------------------------------------------------------- #
#  Planning (dagweergave in routeblokken, zoals het vertrouwde overzicht)
# --------------------------------------------------------------------------- #
@bp.route("/planning")
def planning():
    guard = login_required("view_planning")
    if guard:
        return guard
    day = request.args.get("day", _today_iso())
    d = datetime.strptime(day, "%Y-%m-%d").date()
    conn = db()
    monteurs = conn.execute("SELECT * FROM monteurs WHERE active=1 ORDER BY id").fetchall()
    jobs = conn.execute("""
        SELECT p.*, o.order_number, o.delivery_address, o.city, o.postal, o.email AS o_email,
               o.phone, o.notes, o.volume, o.montage_min, o.amount, o.source, o.service_type,
               o.client_id, c.name AS client
        FROM planning p JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
        WHERE p.date=? ORDER BY p.monteur_id, p.sequence""", (day,)).fetchall()
    unplanned = conn.execute("""SELECT o.*, c.name AS client FROM orders o LEFT JOIN clients c ON c.id=o.client_id
                                WHERE o.status='in_te_plannen' ORDER BY o.desired_date""").fetchall()
    frees = {r["monteur_id"]: r["type"] for r in
             conn.execute("SELECT * FROM free_days WHERE date_from<=? AND date_to>=?", (day, day)).fetchall()}
    all_order_ids = [j["order_id"] for j in jobs] + [o["id"] for o in unplanned]
    items_map = _items_by_order(conn, all_order_ids)

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
            d2["items"] = items_map.get(s["order_id"], "")
            enriched.append(d2)
        routes_by_m[m["id"]] = enriched
        montage = sum((j["montage_min"] or 0) for j in rj)
        coords = [(m["home_lat"], m["home_lng"])] if m["home_lat"] else [BREDA]
        for j in rj:
            coords.append(CITY_COORDS.get(j["city"], BREDA))
        coords.append(BREDA)
        km = sum(haversine(coords[i], coords[i + 1]) for i in range(len(coords) - 1)) if len(coords) > 1 else 0
        alerts = route_alerts(m["id"], bool(rj))
        totals[m["id"]] = {"stops": len(rj), "km": round(km),
                           "time": fmt_duration(montage + km / 45 * 60),
                           "region": region_for([j["city"] for j in rj]),
                           "eta_back": eta_back, "live": bool(live),
                           "alerts": alerts, "delay": sum(a["min"] for a in alerts)}
    conn.close()

    prev_day = (d - timedelta(days=1)).isoformat()
    next_day = (d + timedelta(days=1)).isoformat()
    monday = d - timedelta(days=d.weekday())
    week_days = [monday + timedelta(days=i) for i in range(5)]   # ma t/m vr
    return render_template("planning/planning.html", monteurs=monteurs, routes=routes_by_m, totals=totals,
                           unplanned=unplanned, items=items_map, frees=frees, day=day, dateobj=d,
                           prev_day=prev_day, next_day=next_day, week_days=week_days, today=_today_iso(),
                           can_edit=has_perm("edit_planning"))


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
    conn.close()
    return jsonify(ok=True)


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

    orders = conn.execute("SELECT * FROM orders WHERE status='in_te_plannen' AND is_draft=0 ORDER BY desired_date").fetchall()
    planned = 0
    for o in orders:
        oprov = PROVINCE.get(o["city"])
        best, best_score = None, -1e9
        for di, diso in enumerate(days):
            for m in monteurs:
                if is_free(m["id"], diso):
                    continue
                stops = conn.execute("""SELECT o2.city FROM planning p JOIN orders o2 ON o2.id=p.order_id
                                        WHERE p.monteur_id=? AND p.date=?""", (m["id"], diso)).fetchall()
                cnt = len(stops)
                cap = 8
                if cnt >= cap:
                    continue
                provs = [PROVINCE.get(s["city"]) for s in stops]
                score = 0.0
                if oprov and oprov in provs:
                    score += 120          # zelfde regio als bestaande route die dag
                if oprov and PROVINCE.get(_home_city(m)) == oprov:
                    score += 30           # monteur woont in die regio
                score -= cnt * 7          # spreid de belasting
                score -= di * 3           # liever eerder
                if score > best_score:
                    best_score, best = score, (m["id"], diso, cnt)
        if best:
            mid, diso, seq = best
            mb = conn.execute("SELECT bus_id FROM monteurs WHERE id=?", (mid,)).fetchone()
            conn.execute("""INSERT INTO planning(order_id,monteur_id,bus_id,date,sequence,status,mailed)
                            VALUES(?,?,?,?,?,'gepland',0)""", (o["id"], mid, (mb["bus_id"] if mb else None), diso, seq))
            conn.execute("UPDATE orders SET status='gepland' WHERE id=?", (o["id"],))
            planned += 1
    conn.commit()
    conn.close()
    return jsonify(ok=True, planned=planned)


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
    montage = int(f.get("montage_min") or 30)
    amount = float(f.get("amount") or 0)
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
                    invoice_address,email,desired_date,amount,montage_min,service_type,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (onum, cid, "manual", 0, "gepland", full, city, postal, full, email, day, amount, montage, service, _today_iso()))
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
    conn = db()
    tpl_row = conn.execute("SELECT value FROM settings WHERE skey='tpl_confirm'").fetchone()
    body_tpl = tpl_row["value"] if tpl_row else "Beste {klant}, uw levering is gepland op {datum} ({tijdvak})."
    rows = conn.execute("""SELECT p.id AS pid, p.date, p.slot_start, p.slot_end,
                           o.order_number, o.client_id, c.name AS client
                           FROM planning p JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
                           WHERE p.mailed=0 AND p.status!='afgerond'""").fetchall()
    sent = 0
    for r in rows:
        tijdvak = (r["slot_start"] or "08:00") + "–" + (r["slot_end"] or "17:00")
        try:
            body = body_tpl.format(klant=(r["client"] or "klant"), datum=r["date"], tijdvak=tijdvak,
                                   telefoon="085-0481444", email="info@office-interior.com")
        except Exception:
            body = body_tpl
        conn.execute("""INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment)
                        VALUES(?,?,?,?,?,0)""",
                     (r["client_id"], "out", "Bevestiging van uw levering #" + r["order_number"],
                      body, datetime.now().isoformat(timespec="minutes")))
        conn.execute("UPDATE planning SET mailed=1 WHERE id=?", (r["pid"],))
        sent += 1
    conn.commit()
    conn.close()
    gmail_live = (integ_status("gmail") == "verbonden")
    return jsonify(ok=True, sent=sent, gmail_live=gmail_live)


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
    o = conn.execute("SELECT client_id, email FROM orders WHERE id=?", (oid,)).fetchone()
    if o:
        conn.execute("""INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment)
                        VALUES(?,?,?,?,?,0)""",
                     (o["client_id"], "out", subject, body, datetime.now().isoformat(timespec="minutes")))
        conn.commit()
    conn.close()
    gmail_live = (integ_status("gmail") == "verbonden")
    return jsonify(ok=True, gmail_live=gmail_live,
                   message=("Verzonden via centrale mailbox en bewaard in het klantdossier."
                            if gmail_live else
                            "Opgeslagen in het klantdossier. Zodra de Gmail-koppeling live staat, wordt de mail ook echt verstuurd."))


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
    q += " ORDER BY o.desired_date, o.id DESC"
    rows = conn.execute(q, args).fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) AS n FROM orders GROUP BY status")}
    conn.close()
    return render_template("planning/orders.html", orders=rows, counts=counts, status=status)


@bp.route("/orders/belangrijk")
def important_orders():
    guard = login_required("view_orders")
    if guard:
        return guard
    today = _today_iso()
    conn = db()
    rows = conn.execute("""SELECT o.*, c.name AS client,
                           (SELECT COUNT(*) FROM order_items WHERE order_id=o.id) AS n_items
                           FROM orders o LEFT JOIN clients c ON c.id=o.client_id
                           WHERE o.amount>=? AND o.is_draft=0 AND o.desired_date>=?
                           ORDER BY o.desired_date""", (IMPORTANT_THRESHOLD, today)).fetchall()
    items = _items_by_order(conn, [o["id"] for o in rows])
    conn.close()
    return render_template("planning/important_orders.html", orders=rows, items=items,
                           threshold=IMPORTANT_THRESHOLD)


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
    conn.close()
    return render_template("planning/order_detail.html", o=o, items=items, plan=plan)


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
                 (f.get("name"), f.get("phone"), f.get("email"), int(f.get("speed", 3)),
                  (f.get("bus_id") or None), f.get("home_address"),
                  1 if f.get("active") else 0, mid))
    conn.commit()
    conn.close()
    flash("Monteur bijgewerkt.")
    return redirect(url_for("planning.monteurs"))


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
    conn.close()
    return render_template("planning/busses.html", busses=rows, today=_today_iso(),
                           can_edit=has_perm("manage_users"))


@bp.route("/busses/<int:bid>/edit", methods=["POST"])
def bus_edit(bid):
    if not has_perm("manage_users"):
        abort(403)
    f = request.form
    conn = db()
    conn.execute("""UPDATE busses SET name=?, plate=?, driver=?, max_volume=?, max_weight=?,
                    max_stops=?, apk_date=?, maintenance=?, active=? WHERE id=?""",
                 (f.get("name"), f.get("plate"), f.get("driver"),
                  float(f.get("max_volume") or 0), float(f.get("max_weight") or 0),
                  int(f.get("max_stops") or 0), f.get("apk_date"), f.get("maintenance"),
                  1 if f.get("active") else 0, bid))
    conn.commit()
    conn.close()
    flash("Bus bijgewerkt.")
    return redirect(url_for("planning.busses"))


@bp.route("/free-days", methods=["GET", "POST"])
def free_days():
    guard = login_required("manage_freedays")
    if guard:
        return guard
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
    conn.close()
    return render_template("planning/free_days.html", rows=rows, monteurs=monteurs)


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
        role = request.form.get("role", u["role"])
        perms = [k for k in PERMISSION_KEYS if request.form.get("perm_" + k)]
        active = 1 if request.form.get("active") else 0
        conn.execute("UPDATE users SET role=?, permissions=?, active=? WHERE id=?",
                     (role, json.dumps(perms), active, uid))
        conn.commit()
        conn.close()
        flash("Gebruiker bijgewerkt.")
        return redirect(url_for("planning.users"))
    try:
        current = set(json.loads(u["permissions"] or "[]"))
    except Exception:
        current = set()
    conn.close()
    groups = {}
    for k, label, grp in PERMISSIONS:
        groups.setdefault(grp, []).append((k, label))
    return render_template("planning/user_edit.html", u=u, groups=groups, current=current,
                           roles=ROLE_LABELS, role_defaults=ROLE_DEFAULTS)


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


@bp.route("/deliveries")
def deliveries():
    """Office: leverrapport per monteur (week/maand) + handtekeningenlog (30 dagen)."""
    guard = login_required("view_reports")
    if guard:
        return guard
    cleanup_old_signatures()
    today = datetime.now().date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    month_start = today.replace(day=1).isoformat()
    conn = db()
    monteurs = conn.execute("SELECT id,name FROM monteurs ORDER BY name").fetchall()
    OUTCOMES = ["succesvol", "beschadigd", "niet_thuis"]
    report = []
    for m in monteurs:
        def cnt(since, oc):
            return conn.execute("SELECT COUNT(*) FROM deliveries WHERE monteur_id=? AND ts>=? AND outcome=?",
                                (m["id"], since, oc)).fetchone()[0]
        wk = {oc: cnt(week_start, oc) for oc in OUTCOMES}
        mo = {oc: cnt(month_start, oc) for oc in OUTCOMES}
        if sum(wk.values()) + sum(mo.values()) > 0:
            report.append({"name": m["name"], "week": wk, "month": mo})
    log = conn.execute("""SELECT d.*, o.order_number, c.name AS client, m.name AS monteur
                          FROM deliveries d LEFT JOIN orders o ON o.id=d.order_id
                          LEFT JOIN clients c ON c.id=o.client_id LEFT JOIN monteurs m ON m.id=d.monteur_id
                          ORDER BY d.ts DESC LIMIT 200""").fetchall()
    conn.close()
    return render_template("planning/deliveries.html", report=report, log=log)


# Idempotent initialiseren bij import (ook onder gunicorn).
init_db()
