# Sales Management System Documentation

Enterprise sales management platform with SAP integration. Automates the flow from **Quotations (QU)** to **Sales Orders (SO)** to **Delivery Orders (DO)**.

## Quick Links

| Document | Description |
|----------|-------------|
| [01_INTRODUCTION.md](01_INTRODUCTION.md) | Purpose, scope, audience |
| [02_ARCHITECTURE.md](02_ARCHITECTURE.md) | System diagram, apps, data flow |
| [03_INSTALLATION.md](03_INSTALLATION.md) | Requirements, setup, env vars |
| [04_SAP_SYNC_GUIDE.md](04_SAP_SYNC_GUIDE.md) | SAP sync processes (SO, QU, PO, AR, Finance) |
| [05_MODULES_GUIDE.md](05_MODULES_GUIDE.md) | Module-by-module features |
| [06_API_REFERENCE.md](06_API_REFERENCE.md) | REST endpoints, auth |
| [07_DEPLOYMENT.md](07_DEPLOYMENT.md) | Production checklist, VPS, cron |
| [PPT_OUTLINE.md](PPT_OUTLINE.md) | Slide-by-slide content for presentations |

## Quick Start

```bash
# 1. Clone and enter project
cd salesorder-web/salesorder

# 2. Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env   # Edit .env with your settings

# 5. Run migrations
python manage.py migrate

# 6. Create superuser (optional)
python manage.py createsuperuser

# 7. Run development server
python manage.py runserver
```

Open `http://127.0.0.1:8000` in your browser.

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Django 5.2.3, Django REST Framework |
| Database | SQLite (default), PostgreSQL, MySQL |
| Frontend | Bootstrap 5, Font Awesome |
| PDF | ReportLab, PyPDF2 |
| Excel | OpenPyXL, pandas |
| External | SAP REST API |

## Project Structure

```
salesorder-web/
├── salesorder/              # Django project root
│   ├── salesorder/          # Project settings, urls
│   ├── so/                  # Main app: sales orders, SAP, finance
│   ├── alabama/             # Alabama division portal
│   ├── submittal/           # Submittal PDF builder
│   ├── businesscards/       # Digital business cards
│   ├── tradelicense/        # Trade license notifications
│   ├── requirements.txt
│   └── manage.py
└── docs/                    # This documentation
```
