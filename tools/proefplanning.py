"""Proefplanning: welke routes zou "Automatisch inplannen" maken voor de laatste N orders?

Draait op je eigen computer, buiten OfficeRoute om:
1. haalt de gegevens ALLEEN-LEZEN uit de live database (de verbinding staat op
   read-only, schrijven kan dus niet);
2. zet ze in een tijdelijke kopie op deze computer;
3. rekent daar met exact dezelfde rekenkern als de knop (_autoplan_assign in
   planning_oi.py);
4. maakt er een los HTML-rapport van en opent dat in de browser.
De tijdelijke kopie wordt daarna gewist. De database-URL wordt gevraagd en nergens bewaard.

Gebruik, vanuit de map Planning-O-I:
    python tools/proefplanning.py          laatste 100 orders
    python tools/proefplanning.py 200      laatste 200 orders
"""
import os
import sys
import getpass
import shutil
import sqlite3
import tempfile
import webbrowser
from datetime import date, datetime, timedelta
from decimal import Decimal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(os.path.dirname(REPO), "Proefplanning")

# Wat we uit de live database nodig hebben. Bewust NIET: users, integrations-sleutels,
# settings met tokens, handtekeningen.
COPY_TABLES = ["clients", "orders", "order_items", "monteurs", "busses", "free_days", "products", "geo_cache"]
ROUTE_FIELDS = ("depot", "depart", "max_worktime")      # integrations ikey='route_api', geen sleutels


def _val(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None                                      # bijlagen/afbeeldingen zijn niet nodig
    return v


def fetch_live(url, n):
    import psycopg
    print("Verbinden met de live database (alleen lezen)...")
    src = psycopg.connect(url, options="-c default_transaction_read_only=on", connect_timeout=20)
    src.read_only = True
    data = {}
    with src:
        cur = src.cursor()
        for t in COPY_TABLES:
            if t == "orders":
                cur.execute("""SELECT * FROM orders WHERE is_draft=0 AND COALESCE(delivery_address,'')<>''
                               ORDER BY id DESC LIMIT %s""", (n,))
            elif t == "order_items":
                ocols, orows = data["orders"]
                ids = [r[ocols.index("id")] for r in orows]
                cur.execute("SELECT * FROM order_items WHERE order_id = ANY(%s)", (ids,))
            elif t == "clients":
                cur.execute("SELECT * FROM clients")
            else:
                cur.execute("SELECT * FROM %s" % t)
            cols = [d.name for d in cur.description]
            rows = [tuple(_val(v) for v in r) for r in cur.fetchall()]
            data[t] = (cols, rows)
            print("  %-12s %5d rijen" % (t, len(rows)))
        cur.execute("SELECT field, value FROM integrations WHERE ikey='route_api' AND field = ANY(%s)",
                    (list(ROUTE_FIELDS),))
        data["_route"] = cur.fetchall()
    return data


def fill_local(path, data):
    loc = sqlite3.connect(path)
    for t in COPY_TABLES + ["planning"]:
        loc.execute("DELETE FROM %s" % t)                # demo-gegevens van de lokale start weg
    for t in COPY_TABLES:
        cols, rows = data[t]
        have = {r[1] for r in loc.execute("PRAGMA table_info(%s)" % t).fetchall()}
        keep = [i for i, c in enumerate(cols) if c in have]
        if not keep:
            continue
        sql = "INSERT OR REPLACE INTO %s(%s) VALUES(%s)" % (t, ",".join(cols[i] for i in keep), ",".join("?" * len(keep)))
        loc.executemany(sql, [tuple(r[i] for i in keep) for r in rows])
    loc.execute("DELETE FROM integrations WHERE ikey IN ('route_api','google_maps')")
    for field, value in data["_route"]:
        loc.execute("INSERT INTO integrations(ikey, field, value) VALUES('route_api', ?, ?)", (field, value))
    loc.commit()
    loc.close()


def simulate(P, n):
    conn = P.db()
    try:
        orders = conn.execute("""SELECT o.*, c.name AS client FROM orders o LEFT JOIN clients c ON c.id=o.client_id
                                 WHERE o.is_draft=0 AND COALESCE(o.delivery_address,'')<>''
                                 ORDER BY o.id DESC LIMIT ?""", (n,)).fetchall()
        orders = sorted(orders, key=lambda o: (o["desired_date"] or ""))   # zelfde volgorde als de knop
        # Adressen die nog niet in de adres-cache staan nu opzoeken (PDOK, anders OpenStreetMap).
        todo = [o for o in orders if not conn.execute("SELECT 1 FROM geo_cache WHERE addr_key=? AND lat IS NOT NULL",
                (P._geo_key(P._addr_query(o["delivery_address"], o["postal"], o["city"])),)).fetchone()]
        if todo:
            print("Adressen opzoeken die nog niet bekend waren: %d" % len(todo))
            for i, o in enumerate(todo, 1):
                P._geocode(P._addr_query(o["delivery_address"], o["postal"], o["city"]), "")
                if i % 10 == 0:
                    print("  %d/%d" % (i, len(todo)))
        days, d = [], datetime.now().date() + timedelta(days=1)
        while len(days) < 30:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d += timedelta(days=1)
        print("Inplannen (zelfde rekenkern als de knop)...")
        res = P._autoplan_assign(conn, orders, days, include_existing=False)
        caps = {m["id"]: res["busweights"].get(res["busmap"].get(m["id"]) or 0, 0) for m in res["monteurs"]}
        cfg = P._route_cfg()
    finally:
        conn.close()

    by_id = {o["id"]: o for o in orders}
    day_list, total_km, n_routes, inexact = [], 0.0, 0, 0
    for diso in days:
        routes = []
        for m in res["monteurs"]:
            stops = res["routes"].get((m["id"], diso)) or []
            if not stops:
                continue
            home = res["homes"].get(m["id"]) or res["depot"]
            pts = [home] + [s["coord"] for s in stops] + [res["depot"]]
            km = drive = 0.0
            for a, b in zip(pts, pts[1:]):
                k, mi = P._estimate_leg(a, b)
                km += k
                drive += mi
            montage = sum(s["montage"] for s in stops)
            load = sum(s["weight"] for s in stops)
            provs = []
            for s in stops:
                if s["prov"] and s["prov"] not in provs:
                    provs.append(s["prov"])
            rows = []
            for s in stops:
                o = by_id[s["oid"]]
                exact = s["coord"][3] == "adres"
                inexact += 0 if exact else 1
                rows.append({"num": o["order_number"], "client": o["client"] or "", "city": o["city"] or "",
                             "addr": o["delivery_address"] or "", "montage": round(s["montage"]), "exact": exact,
                             "ml": "O" if o["service_type"] == "ophalen" else ("L" if o["service_type"] == "levering" else "M")})
            cap = caps.get(m["id"], 0)
            routes.append({"name": m["name"], "color": m["color"] or "#0f3d3e", "stops": rows,
                           "km": round(km), "drive": P.fmt_duration(drive), "montage": P.fmt_duration(montage),
                           "total": P.fmt_duration(drive + montage), "provs": provs, "load": round(load),
                           "cap": round(cap), "over": bool(cap and load > cap + P.LOAD_MARGIN_KG),
                           "map": [[round(p[0], 5), round(p[1], 5)] for p in pts],
                           "labels": ["Start: thuis " + m["name"]] + ["%d. %s" % (i + 1, r["client"] or r["city"])
                                                                       for i, r in enumerate(rows)] + ["Eind: depot"]})
            total_km += km
            n_routes += 1
        if routes:
            day_list.append({"label": P._nl_date(diso), "routes": routes,
                             "km": round(sum(r["km"] for r in routes)), "stops": sum(len(r["stops"]) for r in routes)})
    s = {"n": len(orders), "planned": len(res["assign"]), "notfit": len(res["notfit"]), "routes": n_routes,
         "days": len(day_list), "km": "{:,}".format(round(total_km)).replace(",", "."),
         "per_route": ("%.1f" % (len(res["assign"]) / n_routes)).replace(".", ",") if n_routes else "0",
         "inexact": inexact, "workday": P.fmt_duration(res["workday_min"]), "monteurs": len(res["monteurs"]),
         "depot": cfg["depot"], "made": datetime.now().strftime("%d-%m-%Y %H:%M"),
         "first": P._nl_date(days[0])}
    notfit = [{"num": o["order_number"], "client": o["client"] or "", "city": o["city"] or "",
               "montage": round(o["montage_min"] or 0)} for o in res["notfit"]]
    return day_list, s, notfit


REPORT = r"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Proefplanning OfficeRoute</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  :root{--accent:#0f3d3e;--gold:#b88a44;--bg:#f5f7f3;--ink:#16302d;--muted:#7c8a85;--soft:#eef3ec;--line:#e6ebe4;--panel:#fff;--red:#c0533f;--amber:#d9962b}
  *{box-sizing:border-box}
  body{margin:0;font-family:Jost,"Segoe UI",Arial,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5}
  .wrap{max-width:1280px;margin:0 auto;padding:28px 24px 60px}
  h1{margin:0;font-size:28px;letter-spacing:-.02em;color:var(--accent)}
  .sub{color:var(--muted);font-size:14px;margin:4px 0 18px}
  .kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:12px}
  .kpi{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
  .kpi .v{font-size:22px;font-weight:700;color:var(--accent)}.kpi .l{font-size:12px;color:var(--muted)}
  .kpi.warn .v{color:var(--red)}
  .note{font-size:12.5px;color:var(--muted);margin:0 0 22px}
  .day{margin-bottom:26px}
  .day .hd{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}
  .day .hd h2{margin:0;font-size:18px;color:var(--accent)}.day .hd span{font-size:13px;color:var(--muted)}
  .map{height:320px;border-radius:14px;border:1px solid var(--line);overflow:hidden;margin-bottom:12px;background:var(--soft)}
  .rs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
  .r{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
  .r .rh{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:9px 12px;color:#fff}
  .r .rh b{font-size:14px}.r .rh .sp{flex:1}
  .chip{font-size:11.5px;background:rgba(255,255,255,.2);border-radius:999px;padding:2px 8px}
  .chip.bad{background:var(--red);font-weight:700}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);font-weight:600}
  td{padding:5px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  tr:last-child td{border-bottom:0}
  .n{width:22px;color:var(--muted)}.rt{text-align:right;white-space:nowrap}
  .ml{display:inline-block;font-size:10px;font-weight:700;border-radius:5px;padding:0 5px;background:var(--soft);color:var(--accent)}
  .ix{color:var(--amber);font-weight:700}.mut{color:var(--muted)}
  .ft{display:flex;gap:14px;flex-wrap:wrap;padding:7px 10px;font-size:12px;color:var(--muted);background:var(--soft)}
  .ft b{color:var(--ink);font-weight:600}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
  @media(max-width:1000px){.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}.rs{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>Proefplanning</h1>
<div class="sub">Zo zou Automatisch inplannen de laatste {{ s.n }} orders verdelen, op een lege agenda vanaf {{ s.first }}. Gemaakt op {{ s.made }} met de echte gegevens; er is niets gewijzigd in OfficeRoute.</div>
<div class="kpis">
  <div class="kpi"><div class="v">{{ s.planned }}</div><div class="l">van {{ s.n }} orders ingepland</div></div>
  <div class="kpi"><div class="v">{{ s.routes }}</div><div class="l">routes over {{ s.days }} werkdag{{ '' if s.days == 1 else 'en' }}</div></div>
  <div class="kpi"><div class="v">{{ s.per_route }}</div><div class="l">stops per route gemiddeld</div></div>
  <div class="kpi"><div class="v">&asymp; {{ s.km }}</div><div class="l">km in totaal (schatting)</div></div>
  <div class="kpi {{ 'warn' if s.notfit }}"><div class="v">{{ s.notfit }}</div><div class="l">past nergens</div></div>
  <div class="kpi {{ 'warn' if s.inexact }}"><div class="v">{{ s.inexact }}</div><div class="l">adressen alleen op plaats</div></div>
</div>
<p class="note">Zelfde rekenregels als de knop: per order de beste dag en monteur, dezelfde provincie bij elkaar, hooguit {{ s.workday }} per werkdag
  (thuis, alle stops, depot {{ s.depot }}) en binnen het laadgewicht, met {{ s.monteurs }} actieve monteurs en hun vrije dagen.
  Rijtijden en kilometers zijn een schatting over de weg (geen Google). De stopvolgorde is zoals de knop hem zet; met Route optimaliseren wordt die nog korter.
  Alle orders tellen mee alsof ze nog in te plannen zijn, ook als ze al gepland of afgerond zijn. Een &asymp; achter een plaats: exacte locatie onbekend.</p>
{% for d in days %}
<div class="day">
  <div class="hd"><h2>{{ d.label }}</h2><span>{{ d.routes|length }} route{{ '' if d.routes|length == 1 else 's' }} · {{ d.stops }} stops · &asymp; {{ d.km }} km</span></div>
  <div class="map" data-routes='{{ d.routes|tojson }}'></div>
  <div class="rs">
  {% for r in d.routes %}
    <div class="r">
      <div class="rh" style="background:linear-gradient(135deg,{{ r.color }},#0f3d3e)">
        <b>{{ r.name }}</b><span class="chip">{{ r.stops|length }} stops</span><span class="chip">{{ r.provs|join(' · ') or '-' }}</span>
        {% if r.provs|length >= 3 %}<span class="chip bad">{{ r.provs|length }} provincies</span>{% endif %}
        {% if r.over %}<span class="chip bad">overladen</span>{% endif %}
        <span class="sp"></span><span class="chip">&asymp; {{ r.total }}</span>
      </div>
      <table><thead><tr><th class="n">#</th><th>Klant</th><th>Adres</th><th class="rt">Type</th><th class="rt">Montage</th></tr></thead><tbody>
      {% for st in r.stops %}<tr><td class="n">{{ loop.index }}</td><td>{{ st.client or '-' }} <span class="mut">#{{ st.num }}</span></td>
        <td>{{ st.addr }}{% if st.city and st.city not in st.addr %}, {{ st.city }}{% endif %}{% if not st.exact %} <span class="ix" title="Exacte locatie onbekend">&asymp;</span>{% endif %}</td>
        <td class="rt"><span class="ml">{{ st.ml }}</span></td><td class="rt">{{ st.montage }} min</td></tr>{% endfor %}
      </tbody></table>
      <div class="ft"><span>Rijden <b>&asymp; {{ r.drive }}</b></span><span>Montage <b>{{ r.montage }}</b></span><span><b>&asymp; {{ r.km }} km</b></span>{% if r.cap %}<span>Lading <b>{{ r.load }}/{{ r.cap }} kg</b></span>{% endif %}</div>
    </div>
  {% endfor %}
  </div>
</div>
{% endfor %}
{% if notfit %}<div class="card"><h2 style="margin:0 0 6px;font-size:16px">Past nergens ({{ notfit|length }})</h2>
<p class="mut" style="margin:0 0 8px;font-size:13px">Te veel montage- of rijtijd voor één werkdag, of te zwaar voor de bus. Deze moeten met de hand gepland of gesplitst worden.</p>
<table><tbody>{% for o in notfit %}<tr><td>#{{ o.num }}</td><td>{{ o.client or '-' }}</td><td>{{ o.city }}</td><td class="rt">{{ o.montage }} min montage</td></tr>{% endfor %}</tbody></table></div>{% endif %}
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){ if(typeof L==='undefined') return;
  document.querySelectorAll('.map').forEach(function(el){
    var routes=JSON.parse(el.getAttribute('data-routes')||'[]'), all=[];
    var m=L.map(el,{scrollWheelZoom:false}); m.attributionControl.setPrefix(false);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(m);
    routes.forEach(function(r){
      L.polyline(r.map,{color:r.color,weight:4,opacity:.85}).addTo(m).bindTooltip(r.name);
      r.map.forEach(function(p,i){ var end=(i===0||i===r.map.length-1);
        L.circleMarker(p,{radius:end?5:7,color:'#fff',weight:2,fillColor:end?'#b88a44':r.color,fillOpacity:1}).addTo(m).bindTooltip(r.name+': '+r.labels[i]);
        all.push(p); });
    });
    if(all.length) m.fitBounds(all,{padding:[24,24]});
  });
})();
</script></body></html>"""


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    url = os.environ.get("PROEF_DATABASE_URL") or getpass.getpass(
        "Plak de External Database URL uit Render en druk op Enter (wordt niet getoond en niet bewaard): ").strip()
    if not url.startswith(("postgres://", "postgresql://")):
        sys.exit("Dat lijkt geen database-URL (die begint met postgresql://).")
    data = fetch_live(url, n)
    url = None

    tmp = tempfile.mkdtemp(prefix="proefplanning-")
    try:
        # Lokale kopie: OfficeRoute laden op SQLite in de tijdelijke map, zonder live database,
        # zonder Radius en zonder Google-sleutel.
        os.environ["PLANNING_OI_DB_PATH"] = os.path.join(tmp, "proef.db")
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("GOOGLE_MAPS_API_KEY", None)
        os.environ["VELOCITY_DISABLE"] = "1"
        sys.path.insert(0, REPO)
        import planning_oi as P
        fill_local(os.environ["PLANNING_OI_DB_PATH"], data)
        P._GCACHE["products"] = None
        P._GCACHE["settings"] = None
        days, s, notfit = simulate(P, n)

        from jinja2 import Environment
        html = Environment(autoescape=True).from_string(REPORT).render(days=days, s=s, notfit=notfit)
        os.makedirs(OUT_DIR, exist_ok=True)
        out = os.path.join(OUT_DIR, "proefplanning-%s-laatste-%d.html" % (datetime.now().strftime("%Y-%m-%d-%H%M"), n))
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(html)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nKlaar: %d van %d orders in %d routes, %d passen nergens." % (s["planned"], s["n"], s["routes"], s["notfit"]))
    print("Rapport: " + out)
    webbrowser.open("file:///" + out.replace("\\", "/"))


if __name__ == "__main__":
    main()
