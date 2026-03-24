# Django project

This directory is the **Django project root** (`manage.py`, `requirements.txt`, apps).

- **Repository overview & GitHub home:** [../README.md](../README.md)
- **Full documentation:** [../docs/README.md](../docs/README.md)

## Run locally

```bash
cd salesorder   # from repo root: salesorder-web/salesorder
python -m venv venv
venv\Scripts\activate   # Windows
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

For setup details, environment variables, and deployment, use the [documentation index](../docs/README.md).
