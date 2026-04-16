# 🍔 McdShiftSync

Automatically sync your McDonald's work schedule to Google Calendar (and Apple Calendar via Google Calendar subscription).

McdShiftSync is a self-hosted web application that lets McDonald's employees link their [mymcd.eu](https://mymcd.eu) account to Google Calendar. Once set up, a background worker automatically syncs shifts every 2 hours — no manual work needed.

🌐 **Live at [mymymcd.eu](https://mymymcd.eu)**

## Features

- **Automatic shift sync** — fetches your shifts from mymcd.eu and creates Google Calendar events every 2 hours
- **Smart deduplication** — only creates/deletes events that actually changed, avoiding unnecessary API calls
- **Premium features** — optionally shows shift managers (RS/OS/NS) and tracked coworkers in event descriptions
- **Multi-language UI** — supports English, Czech, and Ukrainian
- **Google OAuth login** — secure authentication, no passwords stored on the server for Google
- **Self-hosted via Docker** — runs on your own server with full control over your data

## How It Works

1. User logs in with their Google account
2. User links their McDonald's (mymcd.eu) credentials
3. The background worker periodically fetches shifts via the MyMcdAPI and syncs them to a dedicated Google Calendar
4. Changes are detected automatically — new shifts are added, removed shifts are deleted

## Prerequisites

- **Python 3.11+**
- **Docker** and **Docker Compose** (for production deployment)
- **Google Cloud project** with OAuth 2.0 credentials configured:
  - Enable the **Google Calendar API** and **Google Identity API**
  - Create OAuth 2.0 Client ID (Web application type)
  - Set authorized redirect URI to `https://yourdomain.com/oauth2callback`
- A McDonald's employee account at [mymcd.eu](https://mymcd.eu)

## Tech Stack

- **Backend**: Python, Flask, Gunicorn
- **Database**: SQLite (via Flask-SQLAlchemy)
- **McDonald's API**: [MyMcdAPI](https://github.com/Milos-Opletal/MyMcdAPI) — a Python wrapper for the mymcd.eu employee portal
- **Google Calendar**: google-api-python-client, google-auth-oauthlib
- **Deployment**: Docker, Docker Compose

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/Milos-Opletal/McdShiftSync.git
cd McdShiftSync
```

### 2. Create a `.env` file

```bash
cp .env.example .env
```

Fill in your credentials:

```env
# Manager account credentials (used for global shift overview sync)
MANAGER_EMAIL=your-manager@email.com
MANAGER_PASSWORD=your-password

# Google OAuth 2.0 (from Google Cloud Console)
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
GOOGLE_PROJECT_ID=your-project-id
GOOGLE_REDIRECT_URI=https://yourdomain.com/oauth2callback
```

### 3. Build and run with Docker

```bash
docker build -t mcd-shift-sync:latest .
docker compose up -d
```

This starts two services:
- **web** — the Flask web app served by Gunicorn on port `4330`
- **worker** — the background sync process that runs every 2 hours

### 4. Running locally (development)

```bash
pip install flask flask-sqlalchemy google-api-python-client google-auth google-auth-oauthlib pytz requests beautifulsoup4 gunicorn

# Start the web app
python webapp/app.py

# In a separate terminal, start the sync worker
python sync.py
```

## Project Structure

```
McdShiftSync/
├── webapp/
│   ├── app.py              # Flask web application (routes, OAuth flow)
│   ├── db_manager.py       # SQLAlchemy models and database helpers
│   ├── translations.py     # UI translations (EN, CS, UK)
│   ├── templates/          # Jinja2 HTML templates
│   └── static/             # CSS, JS, images
├── sync.py                 # Background sync worker
├── MyMcdAPI.py             # McDonald's API wrapper
├── McdShiftManager.py      # Shift data manager (coworkers, special roles)
├── Dockerfile
├── docker-compose.yml
└── .env                    # Your credentials (not committed)
```

## Acknowledgements

This project uses [MyMcdAPI](https://github.com/Milos-Opletal/MyMcdAPI) to communicate with the mymcd.eu employee portal.
