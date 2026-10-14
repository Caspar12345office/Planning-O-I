# Projectcontext voor Claude

Dit bestand is de projectkennis voor elke Claude-sessie, ook in de cloud of vanaf de
iPhone. Werk de sectie "Stand van zaken" bij aan het eind van een sessie waarin iets
veranderde. **Zet hier nooit wachtwoorden, sleutels, tokens of database-adressen in:
die staan alleen als environment variable in Render.**

## Wie en wat

Office-Interior (kantoormeubilair, Breda). Caspar is de beheerder en opdrachtgever.
Vier Flask-apps, elk met een eigen GitHub-repo (organisatie `Caspar12345office`) en een
eigen Render-service in Frankfurt, op **één gedeelde PostgreSQL-database**:

| App | Repo | Hoofdbestand | Live |
|---|---|---|---|
| Kantoor (OfficeRoute) | `Planning-O-I` | `planning_oi.py` | https://planning-o-i-fra.onrender.com |
| Monteur-app | `OfficeRoute-Mobile-app` | `monteur.py` | https://officeroute-monteur-fra.onrender.com |
| Magazijn-app | `OfficeRoute-Magazijn-app` | `magazijn.py` | https://officeroute-magazijn-fra.onrender.com |
| OfficeHub + Bedrijfsbank + Argus | `OfficeHub` | `hub.py`, `kennisbank.py`, `argus.py` | https://www.office-interiorhub.nl |

- "OR" in de chat betekent OfficeRoute. In UI, code en commits altijd voluit "OfficeRoute".
- Gedeelde `users`-tabel, 2FA via een authenticator-app (TOTP). Zonder gekoppelde 2FA
  koppelt wie als eerste inlogt de authenticator.
- Single sign-on: de hub-tegels lopen via `/go`, dat een kort geldig HMAC-token meegeeft;
  elke app verifieert dat met dezelfde env `SSO_SECRET`.
- De kantoor-app vult de database (seed); de andere apps seeden alleen lokaal op SQLite.
- **De monteur-app die monteurs echt gebruiken is `OfficeRoute-Mobile-app`.** De
  `/monteur`-view in `Planning-O-I` is iets anders; daar is eerder per ongeluk in gebouwd.

## Werkafspraken met Caspar

- Antwoorden en UI-teksten in het Nederlands, je-vorm, kort.
- **Nooit een em dash of en dash** (de lange en halflange streepjes), ook niet in code,
  commits, mails of UI. Gebruik een komma, dubbele punt, haakjes of "tot".
- Geen emoji in de UI of navigatie.
- Lichte huisstijl: teal `#0f3d3e`, goud `#b88a44`, lichte achtergrond, witte panelen.
  Geen donkere of fotorijke ontwerpen. Zie de skill `officeroute-huisstijl` in
  `.claude/skills/`.
- **Bij een nieuw of ingrijpend scherm eerst een mockup laten zien** en akkoord vragen.
  Bij creatieve opdrachten (animatie, "maak iets moois") eerst om een concrete
  referentie vragen in plaats van concepten te gokken.
- Caspar houdt van rustige, continue beweging (zoals het deeltjesweb op de hub), niet
  van drukte.
- Commitberichten in het Nederlands: korte titel, daaronder waarom.
- Eerlijk melden wat wel en niet getest is.

## Deploy

Commit en push naar `main`, Render bouwt dan automatisch (1 tot 8 minuten). Controleer
met `curl <app-url>/version`: dat geeft de korte commit-hash die live draait.
- **`main` is direct live voor alle collega's.** Werk je in een cloudsessie op een eigen
  branch, vraag Caspar dan of hij wil samenvoegen naar `main`.
- Secrets en instellingen staan alleen als environment variable in Render. Een env-var
  via de Render-API zetten herstart de service niet: daarna handmatig deployen.

## Lokaal testen

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
PLANNING_OI_DB_PATH=/tmp/test.db python -c "..."   # SQLite met demo-seed
```
- Test met de Flask test-client. Inloggen in een test: `session["p_user_id"] = <id>`
  via `client.session_transaction()`.
- Kantoor: `VELOCITY_DISABLE=1` zet de Radius-achtergrondtaak uit tijdens tests.
- Templates worden niet automatisch herladen: herstart de server na een wijziging.

## Valkuilen die eerder echt tijd kostten

- SQLite naar PostgreSQL loopt via de `_xlate`-shim: `?` wordt `%s` (buiten
  string-literals), `GROUP_CONCAT` wordt `string_agg`, `BLOB` wordt `BYTEA`. Tabellen
  zonder `id`-kolom horen in `_NO_ID_TABLES`. Houd de shim in alle apps gelijk.
- `ALTER TABLE ... ADD COLUMN x INTEGER DEFAULT 0` vult bestaande rijen met 0, niet
  NULL. Eenmalige backfill dus via een vlag in `settings`.
- Een nieuwe standaardtekst in `MAIL_TEXT_DEFAULTS` doet niets als de oude tekst al in
  `settings` staat.
- Resend (mail): elke aanroep met een eigen `User-Agent`, anders weigert Cloudflare met
  403 / error 1010. "E-mails écht versturen" staat bewust uit tot Caspar het aanzet.
- Mobiel: `100svh`, niet `dvh` (met dvh beweegt de hele app mee met de browserbalk).
- Inline `<svg>` altijd een expliciete breedte en hoogte geven, anders onzichtbaar.
- `loading="lazy"` in een blok met `display:none` laadt nooit, zonder foutmelding.
- HTML krijgt `Cache-Control: no-store`; iOS en de PWA hielden anders oude versies vast.
- Jinja: nooit een dict-key `items` (botst met `.items()`).
- Een class `app` botst met de globale `.app{min-height:100vh}`.
- Verse Render-build: de psycopg-pool valideren met snelle fallback; geen `--preload`.
- Render draait op UTC. Elke `app.py` zet daarom bij de start `TZ` op Nederlandse tijd
  (POSIX-regel + `time.tzset()`), zodat `datetime.now()` overal NL-tijd is. Niet weghalen:
  de apps delen tijdstempels via de database en moeten dezelfde klok gebruiken.
- Web-apps krijgen op de achtergrond geen GPS (Waze, scherm op slot). Daarom komen de
  busposities uit Radius Velocity (in de kantoor-app). Caspar wil geen App Store-app.

## Stand van zaken (bijwerken)

Laatst bijgewerkt: 2026-10-14.

Gedaan op 2026-10-14: alle apps rekenen in Nederlandse tijd (TZ in `app.py`). Daarvoor liep
alles op Render 2 uur achter: AT-tijden in de planning, ETA in de "onderweg"-mail, tijdstempels
van notities, gestart/afgerond, uren. Oude rijen in de database van voor 2026-10-14 staan nog
in UTC (niet omgezet).

Uitrol naar ongeveer 50 collega's in vier stappen:
1. Klaarmaken: **af** (Sentry-code in alle apps, versiecode van de inlogpagina's, onaffe
   hub-tegels alleen voor beheerders, achtergrondtabbladen pollen niet + korte cache,
   controle op startwachtwoorden en 2FA op de Gebruikers-pagina).
2. Echte gegevens invullen (bussen, monteurs, montagetijden, accounts): Caspar.
3. Proefgroep 1 tot 2 weken, monteurfuncties naar de echte monteur-app overzetten
   (stepper, GPS-startplicht, meten), "mijn account"-pagina.
4. Uitrol naar iedereen, oude Oregon-database opruimen.

Gedaan op 2026-10-12: echte routes en reistijden (Google, schatting zonder sleutel);
verlof goedkeuren als recht `approve_leave` + "Mijn aanvragen" in monteur- en magazijn-app;
Radius Velocity werkt tegen de echte API (refresh token inwisselen, Telematics-account,
bussen op de dashboardkaart, kilometers per voertuig als schatting); donkere modus kantoor
(keuze onderin het menu); monteurs toevoegen + automatische monteurskaart bij rol Monteur.
Later op 2026-10-12: stops slepen in de planning (gouden lijn, `/api/reorder`, tijden worden
herberekend) met een melding in de monteur-app bij een wijziging van de route van vandaag
(tabel `monteur_notices`); routes per monteur inklappen; monteur geeft uren tot 31 dagen terug
door (urenregister toont "achteraf doorgegeven"); handtekeningen wit in donkere modus
(`.sigbox`); donkere modus in de hub; Leaflet-logo van de kaarten weg (de kleine
OpenStreetMap-vermelding is verplicht en blijft).
Ook op 2026-10-12: databaseverbindingen robuuster in alle apps (controle bij uitlenen,
vergeten verbindingen na elk verzoek terug, grotere pool) tegen de terugkerende 500's;
monteur- en magazijn-app loggen nu in via het SSO-token van de hub; "Meekijken als
monteur" voor beheerders (`/meekijken`, alleen kijken, elke POST geweigerd); route van
morgen in de monteur-app (`?dag=morgen`, laadlijst, bewust oranje en zonder knoppen);
donkere modus monteur-app leesbaar (`--accent` is in donker licht).

Open:
- Google Maps-sleutel nog niet ingevuld (Koppelingen > Google Maps): tot dan is reistijd een
  schatting met "≈". Niet getest met een echte sleutel.
- Radius: Caspar moet het refresh token zelf bij Koppelingen zetten en de 9 bussen met
  kenteken onder Bussen zetten (km tellen ook zonder, maar CO2 alleen voor bekende bussen).
- Na een deploy geeft de kantoor-app soms één keer een 500 op /login (oorzaak onbekend;
  traceback staat in Sentry of Koppelingen > Live status > Backend Python).
- Bedrijfsbank: documenten bij register-items alleen als download, nog geen "bekijken".
- `SENTRY_DSN` nog zetten op monteur-, magazijn- en hub-service.
- 12 accounts staan nog op het gedeelde startwachtwoord.
- `SSO_SECRET` controleren op monteur- en magazijn-service (gelijk aan de hub), anders
  geeft de hub-knop nog het inlogscherm.
- 500's: niet bevestigd dat de poolfix de oorzaak was (geen traceback gezien). Blijven
  ze, dan Sentry of Koppelingen > Python-blok bekijken.

Verbeterlijst (nog niets gekozen): testomgeving, eigen subdomeinen, "mijn account"-pagina,
één kleurpalet in alle apps, eigen 404/500-pagina's, "wat is er nieuw"-melding, lege
staten, handleiding bijwerken, UptimeRobot, backup-terugzettest.

## Deze repo: kantoor-app (OfficeRoute)

- `app.py`: Flask-app met Sentry en gzip. `planning_oi.py` (ongeveer 8000 regels): alles
  in de blueprint `planning` op de root. Templates in `templates/planning/`, basis
  `plan_base.html` (iconen-sprite, `oiPoll`-helper: pollen alleen als het tabblad zichtbaar is).
- Rechten: `PERMISSIONS` en `ROLE_DEFAULTS`; pagina's met `login_required("<recht>")`,
  API's met `has_perm`. Navigatie in `NAV`.
- Koppelingen: `INTEGRATIONS` + `integration_test`; commandocentrum `/koppelingen` via
  `_connection_health` en `_conn_advice`.
- Klantmails: `_mail_html` / `_mail_text`, teksten in `MAIL_TEXT_DEFAULTS` (gelijk houden
  met `MAIL_PREVIEW_BLOCKS`). Volgpagina `/track/<token>`; de kaart gaat pas 60 minuten
  voor het tijdvak open.
- Live locatie: tabel `monteur_location` (telefoon via de monteur-app, bussen via Radius);
  `_live_loc`, `LIVE_MAX_MIN`. Radius Velocity staat in het blok direct na `_live_loc`.
  Radius-token = refresh token; `_velocity_access` wisselt het in voor een toegangstoken (30 dagen,
  bewaard in settings `velocity_access`, na 25 dagen of bij een 401 opnieuw).
  Velocity-eigenaardigheden (gezien 2026-10-12): meerdere accounts per bedrijf (Fuel en
  Telematics; alleen Telematics heeft busposities), lang intern ID naast klantnummer (47057),
  kenteken met naam ervoor ('Tom-VVB-14-T', zie `_split_registration`), en het veld `timestamp`
  loopt een uur voor; de leesbare `time` is Nederlandse tijd en wordt gebruikt. Geparkeerde
  bussen melden zich niet (contact uit), dus de kaart toont ze tot 12 uur na de laatste melding.
  Daarnaast bewaart de Radius-ophaalronde alle bussen in `bus_location` (privéritten niet);
  `/api/bus-locations` zet ze als busjes op de dashboardkaart (alleen kantoor).
- Pollen: `_poll_cached` (korte cache per proces) voor planningversie en online-lijst.
- Donkere modus: keuze Licht/Donker/Systeem onderin het menu (localStorage `oi-kantoor-theme`,
  `data-theme="dark"` op html). Basis via de kleurvariabelen in `plan_base.html`; per scherm een
  blok "donker (gegenereerd)" in page_css voor vaste kleuren. Nieuwe vaste kleuren in een scherm?
  Liever variabelen gebruiken, of een `[data-theme="dark"]`-regel toevoegen.
- Shopify-webhook `/api/shopify/webhook`; ochtendmail `/cron/ochtendmail` via cron-job.org.
  Orders met de tag `geen-officeroute` slaat de webhook over (`_shopify_skip`). Bij een
  handmatige order zet je die tag op de conceptorder; Shopify neemt hem mee naar de order.
- Gebruikers: `_start_passwords` markeert accounts met een gedeeld startwachtwoord.
- Verlof: goedkeurder = beheerder, of het recht `approve_leave` ("Verlofaanvragen goedkeuren",
  per gebruiker onder Gebruikers), of als vangnet een adres uit `APPROVERS_MONTEUR` /
  `APPROVERS_OFFICE` (`is_leave_approver`). Goedkeurders zien alle open aanvragen, een eigen
  aanvraag zonder knoppen. Monteur- en magazijn-app tonen "Mijn aanvragen" met status en de
  echte namen bij "Gaat naar" (`_leave_approver_names`, gelijk houden met de kantoor-app).
- Routes: blok "Echte adressen en echte routes" na de Radius-koppeling. `_geocode`,
  `_order_point`, `_home_point`, `_depot_point`, `_route` (Google of schatting, met cache),
  `_planned_route` (planningpagina, kaart, terugkomst), `compute_arrivals` (live: vanaf de
  positie van nu, per monteur hooguit elke 20 minuten een Google-route). "Route
  optimaliseren" en de klokknop zetten de geplande tijden (GT) met echte rijtijden.
  `route_alerts` toont alleen echte filevertraging (met verkeer min zonder verkeer).
  Kosten: met verkeer is het de Pro-soort van Google (5.000 per maand gratis).
