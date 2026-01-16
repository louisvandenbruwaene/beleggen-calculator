# Beleggingen Belastingcalculator

Belgische meerwaardebelasting calculator voor beleggers. Berekent hoeveel aandelen je kunt verkopen binnen de belastingvrijstelling.

## Tech Stack

- **Backend**: Flask + SQLAlchemy
- **Database**: PostgreSQL (Railway) / SQLite (lokaal)
- **Frontend**: Vanilla JS + Chart.js
- **Hosting**: Railway

## Belgische Belastingregels

- **Vrijstelling**: €10.000/jaar belastingvrij (basis)
- **Overdracht**: Eerste €1.000 ongebruikte vrijstelling draagt over naar volgend jaar
- **Maximum**: €15.000 (na 5 jaar zonder gebruik)
- **Tarief**: 10% op meerwaarde boven vrijstelling
- **Getrouwde koppels**: Verdubbeling van alle limieten (€20.000 basis, €2.000 overdracht, max €30.000)
- **Principe**: FIFO (First In, First Out)

## Structuur

```
website/
├── app.py              # Flask applicatie + alle routes
├── templates/
│   ├── base.html       # Layout template
│   ├── index.html      # Landing page
│   ├── login.html      # Login pagina
│   ├── register.html   # Registratie pagina
│   ├── dashboard.html  # Gebruikers dashboard
│   ├── portfolio.html  # Portfolio beheer
│   ├── calculator.html # Belasting calculator
│   └── settings.html   # Account instellingen
├── static/
│   ├── css/style.css   # Styling
│   └── js/app.js       # Frontend JS
├── railway.toml        # Railway configuratie
├── Procfile            # Gunicorn start command
└── requirements.txt    # Python dependencies
```

## Database Models

- **User**: Account met username, email, password_hash
- **Portfolio**: Verzameling assets per gebruiker
- **Asset**: Aandeel/ETF met ISIN, gekoppeld aan portfolio
- **Lots**: Aankopen (datum, aantal, prijs) - encrypted opgeslagen

## Environment Variables (Railway)

```
DATABASE_URL=postgresql://...    # Automatisch door Railway PostgreSQL addon
SECRET_KEY=<random string>       # Flask session signing
ENCRYPTION_KEY=<fernet key>      # Lot data encryptie
```

## Railway CLI

```bash
railway login                    # Inloggen
railway link                     # Project koppelen
railway logs                     # Logs bekijken
railway variables                # Env vars bekijken
railway up                       # Deployen
```

## Lokaal Draaien

```bash
cd website
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py                    # http://localhost:5000
```

## Recente Fixes

- **2026-01**: Overdracht systeem toegevoegd (€1.000/jaar, max €15.000)
- **2026-01**: Getrouwde koppels optie toegevoegd (verdubbelt alle limieten)
- **2026-01**: UI verduidelijkt: eenmalige calc vs meerjarenplan
- **2026-01**: Security hardening (session cookies, input validatie, division by zero fixes)
- **2026-01**: Race condition in registratie gefixt
- **2026-01**: "Gratis" verwijderd - app is altijd gratis
