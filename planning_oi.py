"""
Planning O-I -- planningssysteem voor meubelbezorging & montage.

Een zelfstandige Flask-blueprint met een eigen database, eigen rollen/rechten en
eigen login. Draait naast Follow O-I op dezelfde Render-service.

Status: productieklaar als testopstelling. Alle schermen werken met echte data en
echte interacties; de externe koppelingen (Shopify, Gmail, Google Maps, Route API,
Google OAuth/MFA) hebben volledig ingerichte instelschermen en staan klaar om met
echte API-logica te worden "ingeplugd".
"""

from flask import (
    Blueprint, render_template, request, redirect, url_for, session,
    flash, jsonify, Response, abort,
)
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, os, json, secrets, csv, io
from datetime import datetime, timedelta, date

bp = Blueprint(
    "planning",
    __name__,
    url_prefix="",          # eigen Render-service: app draait op de root-URL
    template_folder="templates",
)

# Eigen database; respecteert de persistente schijf van Render (zie render.yaml).
DB_PATH = os.environ.get("PLANNING_OI_DB_PATH", "planning_oi.db")
# Het bedrijf laadt/lost altijd in Breda (alle routes starten en eindigen daar).
HOME_BASE = "Breda"


# --------------------------------------------------------------------------- #
#  Database
# --------------------------------------------------------------------------- #
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
#  Rollen & rechten (volledig configureerbaar per gebruiker)
# --------------------------------------------------------------------------- #
# Elke rechten-sleutel met een leesbaar label en de groep waarin hij hoort.
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
    ("view_reports",       "Rapportages bekijken",       "Rapportage"),
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
    "manager": [
        "view_kpis", "view_planning", "view_reports", "view_orders",
        "view_invoices", "view_personnel", "export", "view_emails",
    ],
    "planner": [
        "view_planning", "edit_planning", "plan_orders", "assign_monteurs",
        "edit_routes", "optimize_routes", "inform_clients", "manage_freedays",
        "view_reports", "view_orders",
    ],
    "administratie": [
        "view_orders", "edit_clients", "view_emails", "view_invoices",
        "view_planning", "complete_deliveries",
    ],
    "monteur": ["monteur_app"],
}
ROLE_LABELS = {
    "beheerder": "Beheerder",
    "manager": "Manager",
    "planner": "Planner",
    "administratie": "Administratie",
    "monteur": "Monteur",
}


# --------------------------------------------------------------------------- #
#  Koppelingen (integraties) -- alles staat klaar om in te stellen
# --------------------------------------------------------------------------- #
# Elke koppeling met de velden die ingevuld moeten worden. type bepaalt het
# invoerveld in de UI (text / password / toggle / select).
INTEGRATIONS = [
    {
        "key": "shopify", "name": "Shopify", "icon": "🛍",
        "desc": "Realtime import van bevestigde orders als 'Nog in te plannen'.",
        "fields": [
            {"key": "shop_url", "label": "Shop-URL", "type": "text", "placeholder": "office-interior.myshopify.com"},
            {"key": "api_key", "label": "API-sleutel", "type": "password"},
            {"key": "api_secret", "label": "API-secret", "type": "password"},
            {"key": "access_token", "label": "Admin access token", "type": "password"},
            {"key": "webhook_secret", "label": "Webhook-secret", "type": "password"},
            {"key": "import_drafts", "label": "Draft orders importeren", "type": "toggle", "default": "0",
             "lock_off": True, "help": "Beveiligd: draft orders worden NOOIT automatisch geïmporteerd."},
            {"key": "auto_sync", "label": "Automatisch synchroniseren", "type": "toggle", "default": "1"},
        ],
    },
    {
        "key": "gmail", "name": "Gmail (centrale mailbox)", "icon": "✉",
        "desc": "Toon volledige e-mailhistorie per klant vanuit één centrale mailbox.",
        "fields": [
            {"key": "mailbox", "label": "Centrale mailbox", "type": "text", "placeholder": "planning@office-interior.nl"},
            {"key": "client_id", "label": "OAuth client-ID", "type": "password"},
            {"key": "client_secret", "label": "OAuth client-secret", "type": "password"},
            {"key": "label_filter", "label": "Labelfilter (optioneel)", "type": "text", "placeholder": "Bezorging"},
        ],
    },
    {
        "key": "google_maps", "name": "Google Maps", "icon": "🗺",
        "desc": "Kaarten, live locatie en navigatie in de monteur-app.",
        "fields": [
            {"key": "api_key", "label": "Maps API-sleutel", "type": "password"},
        ],
    },
    {
        "key": "route_api", "name": "Route Optimization", "icon": "🧭",
        "desc": "Automatische routeoptimalisatie (afstand, verkeer, capaciteit, werktijd).",
        "fields": [
            {"key": "provider", "label": "Provider", "type": "select",
             "options": ["Google Route Optimization", "OptaPlanner", "Routific", "Anders"]},
            {"key": "api_key", "label": "API-sleutel", "type": "password"},
            {"key": "max_worktime", "label": "Max. werktijd per dag (uur)", "type": "text", "placeholder": "9"},
            {"key": "depot", "label": "Vertrek/aankomst (depot)", "type": "text", "default": HOME_BASE},
        ],
    },
    {
        "key": "google_oauth", "name": "Google OAuth + MFA", "icon": "🔐",
        "desc": "Inloggen met Google en verplichte multi-factor authenticatie.",
        "fields": [
            {"key": "client_id", "label": "OAuth client-ID", "type": "password"},
            {"key": "client_secret", "label": "OAuth client-secret", "type": "password"},
            {"key": "require_mfa", "label": "MFA verplicht", "type": "toggle", "default": "1"},
            {"key": "allowed_domain", "label": "Toegestaan domein", "type": "text", "placeholder": "office-interior.nl"},
        ],
    },
    {
        "key": "gps", "name": "Live GPS-tracking", "icon": "📍",
        "desc": "Realtime locatie van bussen en veilige klant-trackinglink (Uber/Picnic-stijl).",
        "fields": [
            {"key": "provider", "label": "GPS-provider", "type": "text", "placeholder": "Samsara / Webfleet / app-GPS"},
            {"key": "api_key", "label": "API-sleutel", "type": "password"},
            {"key": "share_precise", "label": "Exacte locatie delen met klant", "type": "toggle", "default": "0",
             "lock_off": True, "help": "Klant ziet altijd alleen een veilige benadering, nooit exacte GPS."},
        ],
    },
    {
        "key": "email", "name": "Klantmail & tracking", "icon": "📨",
        "desc": "Automatische bevestiging, aankomst-tijdvak en trackinglink naar de klant.",
        "fields": [
            {"key": "smtp_host", "label": "SMTP-host", "type": "text", "placeholder": "smtp.office-interior.nl"},
            {"key": "smtp_user", "label": "SMTP-gebruiker", "type": "text"},
            {"key": "smtp_pass", "label": "SMTP-wachtwoord", "type": "password"},
            {"key": "from_name", "label": "Afzendernaam", "type": "text", "default": "Office-Interior Bezorging"},
            {"key": "send_delay_updates", "label": "Automatische vertraging-updates", "type": "toggle", "default": "1"},
        ],
    },
    {
        "key": "backup", "name": "Back-ups", "icon": "💾",
        "desc": "Automatische dagelijkse back-up van de volledige database.",
        "fields": [
            {"key": "enabled", "label": "Automatische back-up", "type": "toggle", "default": "1"},
            {"key": "frequency", "label": "Frequentie", "type": "select", "options": ["Dagelijks", "Elke 6 uur", "Wekelijks"]},
            {"key": "destination", "label": "Bestemming", "type": "text", "placeholder": "gs://oi-backups of Azure Blob"},
        ],
    },
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
        monteur_id INTEGER, active INTEGER NOT NULL DEFAULT 1, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS clients(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT, phone TEXT,
        address TEXT, postal TEXT, city TEXT, invoice_address TEXT,
        notes TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_number TEXT, client_id INTEGER, source TEXT DEFAULT 'manual',
        is_draft INTEGER DEFAULT 0, status TEXT DEFAULT 'in_te_plannen',
        delivery_address TEXT, invoice_address TEXT, phone TEXT, email TEXT,
        desired_date TEXT, notes TEXT, instructions TEXT,
        volume REAL DEFAULT 0, weight REAL DEFAULT 0, montage_min INTEGER DEFAULT 30,
        shopify_id TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS order_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER, name TEXT, qty INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS monteurs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, phone TEXT, email TEXT,
        skill INTEGER DEFAULT 3, color TEXT, bus_id INTEGER,
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS busses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, plate TEXT, driver TEXT,
        max_volume REAL DEFAULT 12, max_weight REAL DEFAULT 1200,
        max_stops INTEGER DEFAULT 12, apk_date TEXT, maintenance TEXT,
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS planning(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER UNIQUE, monteur_id INTEGER, bus_id INTEGER,
        date TEXT, slot_start TEXT, slot_end TEXT, sequence INTEGER DEFAULT 0,
        status TEXT DEFAULT 'gepland'
    );
    CREATE TABLE IF NOT EXISTS free_days(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        monteur_id INTEGER, type TEXT, date_from TEXT, date_to TEXT, note TEXT
    );
    CREATE TABLE IF NOT EXISTS integrations(
        ikey TEXT, field TEXT, value TEXT, PRIMARY KEY(ikey, field)
    );
    CREATE TABLE IF NOT EXISTS email_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER, direction TEXT, subject TEXT, body TEXT, ts TEXT,
        has_attachment INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS settings(
        skey TEXT PRIMARY KEY, value TEXT
    );
    """)
    conn.commit()

    # Seed alleen wanneer er nog geen gebruikers zijn.
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        _seed(conn)
    conn.close()


def _seed(conn):
    c = conn.cursor()
    now = datetime.now()
    today = now.date()

    def iso(d):
        return d.isoformat()

    # --- bussen ---
    busses = [
        ("Bus 1 - Mercedes Sprinter", "VND-12-A", "Rick", 14, 1400, 14, iso(today + timedelta(days=120)), ""),
        ("Bus 2 - VW Crafter", "8-XGT-99", "Sven", 12, 1200, 12, iso(today + timedelta(days=40)), "Kleine servicebeurt gepland"),
        ("Bus 3 - Ford Transit", "GV-880-K", "Youssef", 10, 1000, 10, iso(today + timedelta(days=8)), "APK loopt bijna af"),
    ]
    for b in busses:
        c.execute("""INSERT INTO busses(name,plate,driver,max_volume,max_weight,max_stops,apk_date,maintenance)
                     VALUES(?,?,?,?,?,?,?,?)""", b)

    # --- monteurs ---
    monteurs = [
        ("Rick de Vries", "06-21110011", "rick@office-interior.nl", 5, "#0f3d3e", 1),
        ("Sven Bakker", "06-21110022", "sven@office-interior.nl", 4, "#b88a44", 2),
        ("Youssef El Amrani", "06-21110033", "youssef@office-interior.nl", 4, "#15595a", 3),
        ("Daan Hofman", "06-21110044", "daan@office-interior.nl", 3, "#7a5cff", None),
    ]
    for m in monteurs:
        c.execute("""INSERT INTO monteurs(name,phone,email,skill,color,bus_id) VALUES(?,?,?,?,?,?)""", m)

    # --- gebruikers ---
    def mk(name, email, role, monteur_id=None, phone=""):
        c.execute("""INSERT INTO users(name,email,password,role,permissions,phone,monteur_id,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (name, email, generate_password_hash("PlanningOI2025!"), role,
                   json.dumps(ROLE_DEFAULTS[role]), phone, monteur_id, iso(today)))
    mk("Caspar (Beheer)", "beheer@planning-oi.nl", "beheerder", phone="085-0481444")
    mk("Petra Planner", "planner@planning-oi.nl", "planner")
    mk("Manon Manager", "manager@planning-oi.nl", "manager")
    mk("Ad Administratie", "admin@planning-oi.nl", "administratie")
    mk("Rick de Vries", "rick@planning-oi.nl", "monteur", monteur_id=1, phone="06-21110011")

    # --- klanten ---
    clients = [
        ("Gemeente Tilburg", "inkoop@tilburg.nl", "013-5420000", "Stadhuisplein 130", "5038 TC", "Tilburg"),
        ("Brabant Advocaten", "office@brabantadvocaten.nl", "076-5300000", "Claudius Prinsenlaan 12", "4811 DJ", "Breda"),
        ("De Nieuwe Werkplek BV", "facilitair@dnw.nl", "040-2900000", "Kennedyplein 200", "5611 ZT", "Eindhoven"),
        ("Zorggroep West", "inkoop@zorggroepwest.nl", "010-4100000", "Coolsingel 40", "3011 AD", "Rotterdam"),
        ("Studio Noord", "hallo@studionoord.nl", "020-7700000", "Overhoeksplein 1", "1031 KS", "Amsterdam"),
        ("Tech Campus Den Bosch", "fm@techcampus.nl", "073-6100000", "Pettelaarpark 70", "5216 PP", "Den Bosch"),
    ]
    for cl in clients:
        c.execute("""INSERT INTO clients(name,email,phone,address,postal,city,invoice_address,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""", (cl[0], cl[1], cl[2], cl[3], cl[4], cl[5], cl[3], iso(today)))

    # --- orders ---
    # mix van Shopify en handmatig; sommige al gepland, sommige nog in te plannen, 1 draft (mag niet importeren)
    orders = [
        # (num, client_id, source, is_draft, status, addr, city, postal, phone, email, days_from_today, vol, weight, montage, items)
        ("#OI-3041", 1, "shopify", 0, "in_te_plannen", "Stadhuisplein 130", "Tilburg", "5038 TC", "013-5420000", "inkoop@tilburg.nl", 1, 3.2, 280, 60, [("Bureaustoel Pro", 8), ("Vergadertafel 240cm", 1)]),
        ("#OI-3042", 2, "shopify", 0, "in_te_plannen", "Claudius Prinsenlaan 12", "Breda", "4811 DJ", "076-5300000", "office@brabantadvocaten.nl", 2, 1.4, 120, 30, [("Boekenkast eiken", 3)]),
        ("#OI-3043", 3, "manual", 0, "in_te_plannen", "Kennedyplein 200", "Eindhoven", "5611 ZT", "040-2900000", "facilitair@dnw.nl", 2, 5.6, 540, 120, [("Zit-sta bureau", 12), ("Monitorarm", 12)]),
        ("#OI-3044", 4, "shopify", 0, "in_te_plannen", "Coolsingel 40", "Rotterdam", "3011 AD", "010-4100000", "inkoop@zorggroepwest.nl", 3, 2.1, 190, 45, [("Loungebank 3-zits", 2)]),
        ("#OI-3045", 5, "manual", 0, "in_te_plannen", "Overhoeksplein 1", "Amsterdam", "1031 KS", "020-7700000", "hallo@studionoord.nl", 4, 0.9, 60, 20, [("Akoestisch paneel", 6)]),
        ("#OI-3046", 6, "shopify", 1, "draft", "Pettelaarpark 70", "Den Bosch", "5216 PP", "073-6100000", "fm@techcampus.nl", 5, 4.0, 300, 90, [("Phonebooth", 2)]),  # DRAFT: niet importeren
        # reeds gepland
        ("#OI-3038", 2, "shopify", 0, "gepland", "Claudius Prinsenlaan 12", "Breda", "4811 DJ", "076-5300000", "office@brabantadvocaten.nl", 0, 1.8, 150, 40, [("Bureau wit", 4)]),
        ("#OI-3039", 1, "manual", 0, "gepland", "Stadhuisplein 130", "Tilburg", "5038 TC", "013-5420000", "inkoop@tilburg.nl", 0, 2.4, 210, 50, [("Kastenwand", 1)]),
        ("#OI-3040", 4, "shopify", 0, "onderweg", "Coolsingel 40", "Rotterdam", "3011 AD", "010-4100000", "inkoop@zorggroepwest.nl", 0, 1.2, 90, 25, [("Balie-element", 1)]),
    ]
    order_ids = {}
    for o in orders:
        (num, cid, source, draft, status, addr, city, postal, phone, email,
         dft, vol, weight, montage, items) = o
        desired = iso(today + timedelta(days=dft))
        full_addr = f"{addr}, {postal} {city}"
        c.execute("""INSERT INTO orders(order_number,client_id,source,is_draft,status,
                     delivery_address,invoice_address,phone,email,desired_date,volume,weight,montage_min,
                     shopify_id,created_at,notes)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (num, cid, source, draft, status, full_addr, full_addr, phone, email, desired,
                   vol, weight, montage, (f"gid://shopify/Order/{5000+cid}" if source == "shopify" else None),
                   iso(today), ""))
        oid = c.lastrowid
        order_ids[num] = oid
        for nm, q in items:
            c.execute("INSERT INTO order_items(order_id,name,qty) VALUES(?,?,?)", (oid, nm, q))

    # --- planning voor de reeds geplande orders (vandaag) ---
    plan = [
        ("#OI-3038", 1, 1, 0, "08:30", "09:10", "gepland"),
        ("#OI-3039", 1, 1, 1, "09:40", "10:30", "gepland"),
        ("#OI-3040", 2, 2, 0, "08:15", "08:40", "onderweg"),
    ]
    for num, mid, bid, seq, s, e, st in plan:
        c.execute("""INSERT INTO planning(order_id,monteur_id,bus_id,date,slot_start,slot_end,sequence,status)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (order_ids[num], mid, bid, iso(today), s, e, seq, st))

    # --- vrije dagen ---
    c.execute("""INSERT INTO free_days(monteur_id,type,date_from,date_to,note) VALUES(?,?,?,?,?)""",
              (4, "vakantie", iso(today + timedelta(days=2)), iso(today + timedelta(days=9)), "Zomervakantie"))
    c.execute("""INSERT INTO free_days(monteur_id,type,date_from,date_to,note) VALUES(?,?,?,?,?)""",
              (3, "atv", iso(today + timedelta(days=4)), iso(today + timedelta(days=4)), ""))

    # --- e-mailhistorie (Gmail-mock) ---
    c.execute("""INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment) VALUES(?,?,?,?,?,?)""",
              (2, "in", "Vraag over levertijd #OI-3038", "Kunnen jullie 's ochtends leveren?", iso(today), 0))
    c.execute("""INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment) VALUES(?,?,?,?,?,?)""",
              (2, "out", "Re: Vraag over levertijd #OI-3038", "Zeker, we leveren tussen 08:30 en 09:10.", iso(today), 1))

    # --- integratie-defaults ---
    for integ in INTEGRATIONS:
        for f in integ["fields"]:
            if "default" in f:
                c.execute("INSERT OR IGNORE INTO integrations(ikey,field,value) VALUES(?,?,?)",
                          (integ["key"], f["key"], f["default"]))
    # depot vast op Breda
    c.execute("INSERT OR IGNORE INTO integrations(ikey,field,value) VALUES(?,?,?)", ("route_api", "depot", HOME_BASE))

    # --- e-mailtemplates ---
    settings = {
        "company_name": "Office-Interior Bezorging & Montage",
        "home_base": HOME_BASE,
        "tpl_confirm": "Beste {klant},\n\nUw levering is ingepland op {datum} tussen {tijdvak}.\n\nMet vriendelijke groet,\nOffice-Interior",
        "tpl_arrival": "Beste {klant},\n\nOnze monteur is onderweg en arriveert naar verwachting rond {eta}. Volg live: {trackinglink}\n\nOffice-Interior",
    }
    for k, v in settings.items():
        c.execute("INSERT OR IGNORE INTO settings(skey,value) VALUES(?,?)", (k, v))

    conn.commit()


# --------------------------------------------------------------------------- #
#  Auth helpers
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
    try:
        perms = set(json.loads(u["permissions"] or "[]"))
    except Exception:
        perms = set()
    # Beheerder heeft altijd alles.
    if u["role"] == "beheerder":
        return set(ALL_PERMS)
    return perms


def has_perm(perm):
    return perm in user_perms(current_user())


def setting(key, default=""):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE skey=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def integ_value(ikey, field, default=""):
    conn = db()
    row = conn.execute("SELECT value FROM integrations WHERE ikey=? AND field=?", (ikey, field)).fetchone()
    conn.close()
    return row["value"] if row else default


def integ_status(ikey):
    """Bepaal of een koppeling 'klaar' (ingevuld) of nog 'niet gekoppeld' is."""
    integ = INTEGRATION_BY_KEY[ikey]
    conn = db()
    rows = {r["field"]: r["value"] for r in
            conn.execute("SELECT field,value FROM integrations WHERE ikey=?", (ikey,)).fetchall()}
    conn.close()
    # Verplichte (niet-toggle) velden die ingevuld moeten zijn.
    required = [f["key"] for f in integ["fields"] if f["type"] in ("text", "password")]
    filled = [k for k in required if (rows.get(k) or "").strip()]
    if required and len(filled) == len(required):
        return "verbonden"
    if filled:
        return "deels"
    return "niet_gekoppeld"


# Maak helpers beschikbaar in alle blueprint-templates.
@bp.app_context_processor
def _inject():
    if request.blueprint != "planning":
        return {}
    u = current_user()
    return {
        "p_user": u,
        "p_perms": user_perms(u),
        "p_has_perm": has_perm,
        "ROLE_LABELS": ROLE_LABELS,
        "HOME_BASE": HOME_BASE,
        "p_nav": NAV,
    }


def login_required(perm=None):
    """Geeft None terug als toegang ok is, anders een redirect/abort-respons."""
    u = current_user()
    if not u:
        return redirect(url_for("planning.login", next=request.path))
    if perm and perm not in user_perms(u):
        return render_template("planning/no_access.html", perm=perm), 403
    return None


# Navigatiestructuur (label, endpoint, icon, vereist recht). Monteurs zien alleen de app.
NAV = [
    ("Dashboard", "planning.dashboard", "▦", "view_planning"),
    ("Planning", "planning.planning", "🗓", "view_planning"),
    ("Orders", "planning.orders", "📦", "view_orders"),
    ("Routes", "planning.routes", "🧭", "edit_routes"),
    ("Klanten", "planning.clients", "👥", "view_orders"),
    ("Monteurs", "planning.monteurs", "🧰", "view_personnel"),
    ("Bussen", "planning.busses", "🚐", "view_personnel"),
    ("Vrije dagen", "planning.free_days", "🏖", "manage_freedays"),
    ("Rapportages", "planning.reports", "📊", "view_reports"),
    ("Koppelingen", "planning.integrations", "🔌", "manage_integrations"),
    ("Gebruikers", "planning.users", "🔑", "manage_users"),
    ("Instellingen", "planning.company_settings", "⚙", "manage_settings"),
]


# --------------------------------------------------------------------------- #
#  Routes -- auth
# --------------------------------------------------------------------------- #
@bp.route("/")
def home():
    u = current_user()
    if not u:
        return redirect(url_for("planning.login"))
    if u["role"] == "monteur":
        return redirect(url_for("planning.monteur_app"))
    return redirect(url_for("planning.dashboard"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        pw = request.form.get("password") or ""
        conn = db()
        u = conn.execute("SELECT * FROM users WHERE lower(email)=? AND active=1", (email,)).fetchone()
        conn.close()
        if u and check_password_hash(u["password"], pw):
            session["p_user_id"] = u["id"]
            nxt = request.args.get("next")
            if u["role"] == "monteur":
                return redirect(url_for("planning.monteur_app"))
            return redirect(nxt or url_for("planning.dashboard"))
        error = "Onjuiste inloggegevens."
    return render_template("planning/login.html", error=error)


@bp.route("/logout")
def logout():
    session.pop("p_user_id", None)
    return redirect(url_for("planning.login"))


# --------------------------------------------------------------------------- #
#  Routes -- dashboard
# --------------------------------------------------------------------------- #
def _today_iso():
    return datetime.now().date().isoformat()


@bp.route("/dashboard")
def dashboard():
    guard = login_required("view_planning")
    if guard:
        return guard
    conn = db()
    today = _today_iso()
    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
    week_end = (datetime.now().date() + timedelta(days=7)).isoformat()

    def scalar(q, args=()):
        return conn.execute(q, args).fetchone()[0]

    stats = {
        "today": scalar("SELECT COUNT(*) FROM planning WHERE date=?", (today,)),
        "tomorrow": scalar("SELECT COUNT(*) FROM planning WHERE date=?", (tomorrow,)),
        "week": scalar("SELECT COUNT(*) FROM planning WHERE date>=? AND date<=?", (today, week_end)),
        "unplanned": scalar("SELECT COUNT(*) FROM orders WHERE status='in_te_plannen'"),
        "open_orders": scalar("SELECT COUNT(*) FROM orders WHERE status IN('in_te_plannen','gepland')"),
        "underway": scalar("SELECT COUNT(*) FROM planning WHERE status='onderweg'"),
        "monteurs_total": scalar("SELECT COUNT(*) FROM monteurs WHERE active=1"),
        "drafts_blocked": scalar("SELECT COUNT(*) FROM orders WHERE is_draft=1"),
    }
    # monteurs onderweg
    underway = conn.execute("""
        SELECT p.*, m.name AS monteur, m.color, o.order_number, c.name AS client, o.delivery_address
        FROM planning p JOIN monteurs m ON m.id=p.monteur_id
        JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
        WHERE p.status='onderweg'""").fetchall()
    # leveringen vandaag
    today_jobs = conn.execute("""
        SELECT p.*, m.name AS monteur, m.color, o.order_number, c.name AS client, o.delivery_address
        FROM planning p JOIN monteurs m ON m.id=p.monteur_id
        JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
        WHERE p.date=? ORDER BY m.name, p.sequence""", (today,)).fetchall()
    # nog in te plannen
    unplanned = conn.execute("""
        SELECT o.*, c.name AS client FROM orders o LEFT JOIN clients c ON c.id=o.client_id
        WHERE o.status='in_te_plannen' ORDER BY o.desired_date LIMIT 6""").fetchall()
    # bezetting per bus (volume vandaag)
    bus_load = conn.execute("""
        SELECT b.name, b.max_volume, COALESCE(SUM(o.volume),0) AS used
        FROM busses b
        LEFT JOIN planning p ON p.bus_id=b.id AND p.date=?
        LEFT JOIN orders o ON o.id=p.order_id
        WHERE b.active=1 GROUP BY b.id ORDER BY b.name""", (today,)).fetchall()
    conn.close()
    return render_template("planning/dashboard.html", stats=stats, underway=underway,
                           today_jobs=today_jobs, unplanned=unplanned, bus_load=bus_load)


# --------------------------------------------------------------------------- #
#  Routes -- planning (drag & drop, persistent)
# --------------------------------------------------------------------------- #
def _week_dates(anchor=None):
    d = datetime.strptime(anchor, "%Y-%m-%d").date() if anchor else datetime.now().date()
    monday = d - timedelta(days=d.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


@bp.route("/planning")
def planning():
    guard = login_required("view_planning")
    if guard:
        return guard
    week = _week_dates(request.args.get("week"))
    conn = db()
    monteurs = conn.execute("SELECT * FROM monteurs WHERE active=1 ORDER BY id").fetchall()
    busses = conn.execute("SELECT * FROM busses WHERE active=1 ORDER BY id").fetchall()
    # geplande jobs in deze week
    start, end = week[0].isoformat(), week[6].isoformat()
    jobs = conn.execute("""
        SELECT p.*, o.order_number, o.delivery_address, o.volume, o.montage_min,
               c.name AS client FROM planning p
        JOIN orders o ON o.id=p.order_id LEFT JOIN clients c ON c.id=o.client_id
        WHERE p.date>=? AND p.date<=?""", (start, end)).fetchall()
    # vrije dagen deze week
    frees = conn.execute("""SELECT * FROM free_days WHERE date_to>=? AND date_from<=?""",
                         (start, end)).fetchall()
    # nog in te plannen
    unplanned = conn.execute("""
        SELECT o.*, c.name AS client FROM orders o LEFT JOIN clients c ON c.id=o.client_id
        WHERE o.status='in_te_plannen' ORDER BY o.desired_date""").fetchall()
    conn.close()

    # bouw lookup: jobs[monteur_id][date] = [job,...]
    grid = {}
    for j in jobs:
        grid.setdefault((j["monteur_id"], j["date"]), []).append(j)
    free_map = {}
    for f in frees:
        df = datetime.strptime(f["date_from"], "%Y-%m-%d").date()
        dt = datetime.strptime(f["date_to"], "%Y-%m-%d").date()
        for d in week:
            if df <= d <= dt:
                free_map[(f["monteur_id"], d.isoformat())] = f["type"]

    prev7 = (week[0] - timedelta(days=7)).isoformat()
    next7 = (week[0] + timedelta(days=7)).isoformat()
    return render_template("planning/planning.html", week=week, monteurs=monteurs,
                           busses=busses, grid=grid, free_map=free_map, unplanned=unplanned,
                           today=_today_iso(), can_edit=has_perm("edit_planning"),
                           prev7=prev7, next7=next7)


@bp.route("/api/assign", methods=["POST"])
def api_assign():
    if not has_perm("edit_planning"):
        return jsonify(ok=False, error="Geen rechten"), 403
    data = request.get_json(force=True)
    oid = int(data["order_id"])
    mid = int(data["monteur_id"])
    d = data["date"]
    conn = db()
    # standaard bus = bus van monteur
    m = conn.execute("SELECT bus_id FROM monteurs WHERE id=?", (mid,)).fetchone()
    bus_id = m["bus_id"] if m else None
    # volgnummer = aantal jobs die dag voor die monteur
    seq = conn.execute("SELECT COUNT(*) FROM planning WHERE monteur_id=? AND date=?", (mid, d)).fetchone()[0]
    exists = conn.execute("SELECT id FROM planning WHERE order_id=?", (oid,)).fetchone()
    if exists:
        conn.execute("""UPDATE planning SET monteur_id=?, bus_id=?, date=?, sequence=? WHERE order_id=?""",
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
    data = request.get_json(force=True)
    oid = int(data["order_id"])
    conn = db()
    conn.execute("DELETE FROM planning WHERE order_id=?", (oid,))
    conn.execute("UPDATE orders SET status='in_te_plannen' WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
#  Routes -- orders
# --------------------------------------------------------------------------- #
@bp.route("/orders")
def orders():
    guard = login_required("view_orders")
    if guard:
        return guard
    status = request.args.get("status", "")
    conn = db()
    q = """SELECT o.*, c.name AS client,
           (SELECT COUNT(*) FROM order_items WHERE order_id=o.id) AS n_items
           FROM orders o LEFT JOIN clients c ON c.id=o.client_id"""
    args = ()
    if status:
        q += " WHERE o.status=?"
        args = (status,)
    q += " ORDER BY o.desired_date, o.id DESC"
    rows = conn.execute(q, args).fetchall()
    counts = {r["status"]: r["n"] for r in
              conn.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()}
    conn.close()
    return render_template("planning/orders.html", orders=rows, counts=counts, status=status)


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
#  Routes -- klanten + dossier (incl. Gmail-historie)
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
#  Routes -- monteurs, bussen, vrije dagen
# --------------------------------------------------------------------------- #
@bp.route("/monteurs")
def monteurs():
    guard = login_required("view_personnel")
    if guard:
        return guard
    conn = db()
    rows = conn.execute("""SELECT m.*, b.name AS bus FROM monteurs m
                           LEFT JOIN busses b ON b.id=m.bus_id ORDER BY m.name""").fetchall()
    conn.close()
    return render_template("planning/monteurs.html", monteurs=rows)


@bp.route("/busses")
def busses():
    guard = login_required("view_personnel")
    if guard:
        return guard
    conn = db()
    rows = conn.execute("SELECT * FROM busses ORDER BY name").fetchall()
    conn.close()
    return render_template("planning/busses.html", busses=rows, today=_today_iso())


@bp.route("/free-days", methods=["GET", "POST"])
def free_days():
    guard = login_required("manage_freedays")
    if guard:
        return guard
    conn = db()
    if request.method == "POST":
        conn.execute("""INSERT INTO free_days(monteur_id,type,date_from,date_to,note)
                        VALUES(?,?,?,?,?)""",
                     (request.form.get("monteur_id"), request.form.get("type"),
                      request.form.get("date_from"), request.form.get("date_to") or request.form.get("date_from"),
                      request.form.get("note", "")))
        conn.commit()
        flash("Vrije dag geregistreerd.")
    rows = conn.execute("""SELECT f.*, m.name AS monteur FROM free_days f
                           LEFT JOIN monteurs m ON m.id=f.monteur_id
                           ORDER BY f.date_from DESC""").fetchall()
    monteurs = conn.execute("SELECT * FROM monteurs WHERE active=1 ORDER BY name").fetchall()
    conn.close()
    return render_template("planning/free_days.html", rows=rows, monteurs=monteurs)


# --------------------------------------------------------------------------- #
#  Routes -- routes (kaart + geoptimaliseerde stops)
# --------------------------------------------------------------------------- #
@bp.route("/routes")
def routes():
    guard = login_required("edit_routes")
    if guard:
        return guard
    day = request.args.get("day", _today_iso())
    conn = db()
    monteurs = conn.execute("SELECT * FROM monteurs WHERE active=1 ORDER BY id").fetchall()
    jobs = conn.execute("""
        SELECT p.*, o.order_number, o.delivery_address, o.volume, o.montage_min,
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
#  Routes -- rapportages
# --------------------------------------------------------------------------- #
@bp.route("/reports")
def reports():
    guard = login_required("view_reports")
    if guard:
        return guard
    conn = db()
    total_deliveries = conn.execute("SELECT COUNT(*) FROM orders WHERE status='afgerond'").fetchone()[0]
    planned = conn.execute("SELECT COUNT(*) FROM planning").fetchone()[0]
    by_monteur = conn.execute("""SELECT m.name, COUNT(p.id) AS jobs, COALESCE(SUM(o.montage_min),0) AS montage
                                 FROM monteurs m LEFT JOIN planning p ON p.monteur_id=m.id
                                 LEFT JOIN orders o ON o.id=p.order_id
                                 GROUP BY m.id ORDER BY jobs DESC""").fetchall()
    conn.close()
    return render_template("planning/reports.html", total_deliveries=total_deliveries,
                           planned=planned, by_monteur=by_monteur)


# --------------------------------------------------------------------------- #
#  Routes -- koppelingen (integraties)
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
                    val = "0"  # beveiligde toggle blijft altijd uit
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
        return redirect(url_for("planning.integrations", _anchor=ikey))
    # huidige waarden + statussen
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
    st = integ_status(ikey)
    if st == "verbonden":
        return jsonify(ok=True, message="Verbinding gereed. De API-logica kan nu worden ingeschakeld.")
    return jsonify(ok=False, message="Vul eerst alle verplichte velden in om de koppeling klaar te zetten.")


# --------------------------------------------------------------------------- #
#  Routes -- gebruikers, rollen & rechten
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
    # groepeer rechten per groep voor de UI
    groups = {}
    for k, label, grp in PERMISSIONS:
        groups.setdefault(grp, []).append((k, label))
    return render_template("planning/user_edit.html", u=u, groups=groups, current=current,
                           roles=ROLE_LABELS, role_defaults=ROLE_DEFAULTS)


# --------------------------------------------------------------------------- #
#  Routes -- bedrijfsinstellingen + e-mailtemplates
# --------------------------------------------------------------------------- #
@bp.route("/settings", methods=["GET", "POST"])
def company_settings():
    guard = login_required("manage_settings")
    if guard:
        return guard
    conn = db()
    if request.method == "POST":
        for k in ("company_name", "home_base", "tpl_confirm", "tpl_arrival"):
            conn.execute("""INSERT INTO settings(skey,value) VALUES(?,?)
                            ON CONFLICT(skey) DO UPDATE SET value=excluded.value""",
                         (k, request.form.get(k, "")))
        conn.commit()
        flash("Instellingen opgeslagen.")
    vals = {r["skey"]: r["value"] for r in conn.execute("SELECT skey,value FROM settings").fetchall()}
    conn.close()
    return render_template("planning/settings.html", v=vals)


# --------------------------------------------------------------------------- #
#  Routes -- monteur-app (mobiel)
# --------------------------------------------------------------------------- #
@bp.route("/monteur")
def monteur_app():
    guard = login_required("monteur_app")
    if guard:
        return guard
    u = current_user()
    conn = db()
    # bepaal gekoppelde monteur
    mid = u["monteur_id"]
    today = _today_iso()
    jobs = []
    monteur = None
    if mid:
        monteur = conn.execute("SELECT * FROM monteurs WHERE id=?", (mid,)).fetchone()
        jobs = conn.execute("""
            SELECT p.*, o.order_number, o.delivery_address, o.phone, o.instructions,
                   o.montage_min, c.name AS client,
                   (SELECT COUNT(*) FROM order_items WHERE order_id=o.id) AS n_items
            FROM planning p JOIN orders o ON o.id=p.order_id
            LEFT JOIN clients c ON c.id=o.client_id
            WHERE p.monteur_id=? AND p.date=? ORDER BY p.sequence""", (mid, today)).fetchall()
    conn.close()
    return render_template("planning/monteur_app.html", monteur=monteur, jobs=jobs,
                           maps_ready=(integ_status("google_maps") == "verbonden"))


@bp.route("/monteur/complete/<int:pid>", methods=["POST"])
def monteur_complete(pid):
    if not has_perm("monteur_app") and not has_perm("complete_deliveries"):
        abort(403)
    conn = db()
    p = conn.execute("SELECT * FROM planning WHERE id=?", (pid,)).fetchone()
    if p:
        conn.execute("UPDATE planning SET status='afgerond' WHERE id=?", (pid,))
        conn.execute("UPDATE orders SET status='afgerond' WHERE id=?", (p["order_id"],))
        conn.commit()
        flash("Levering afgerond.")
    conn.close()
    return redirect(url_for("planning.monteur_app"))


# Idempotent initialiseren bij import (ook onder gunicorn).
init_db()
