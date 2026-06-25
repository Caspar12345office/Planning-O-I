# Planning O-I

Planningssysteem voor meubelbezorging & montage (Office-Interior). Volledig
losgekoppeld van het Follow O-I portaal: eigen repository, eigen Render-service,
eigen database en eigen login.

## Functionaliteit
- Drag & drop weekplanning (persistent), monteurs × dagen
- Rollen & volledig configureerbare rechten: Beheerder, Manager, Planner, Administratie, Monteur
- Dashboard met KPI's, busbezetting en draft-order-bescherming
- Orders, klantdossier met e-mailhistorie, monteurs, bussen, vrije dagen
- Routes met depot Breda (start/eind) + kaartweergave
- Rapportages (Excel/PDF), bedrijfsinstellingen + e-mailtemplates
- Mobiele monteur-app (route, navigatie via Google/Apple/Waze, levering afronden)
- Koppelingen-module: Shopify, Gmail, Google Maps, Route Optimization,
  Google OAuth+MFA, GPS, klantmail, back-ups — instelschermen staan klaar om de
  echte API-logica in te pluggen. Beveiligde toggles (draft-import, exacte GPS)
  staan vergrendeld.

## Lokaal draaien
```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5059
```

## Deploy op Render
1. Push deze repo naar GitHub.
2. Render → New → Web Service → koppel de repo.
3. Start command: `gunicorn app:app` (staat ook in render.yaml).
4. Voeg een persistente schijf toe op `/var/data` en de env vars `SECRET_KEY`
   (Generate) en `PLANNING_OI_DB_PATH=/var/data/planning_oi.db`.

## Demo-accounts
Wachtwoord `PlanningOI2025!`
- Beheerder: `beheer@planning-oi.nl`
- Planner: `planner@planning-oi.nl`
- Manager: `manager@planning-oi.nl`
- Administratie: `admin@planning-oi.nl`
- Monteur: `rick@planning-oi.nl`
