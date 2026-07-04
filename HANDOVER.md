# Project Handover Document — Sales Management System

> **Who is this for?** The new developer taking over this project.
> **What is this document?** A plain-English, top-to-bottom explanation of the whole
> system: what it does, how the code is organised, how data flows, how to run it, and
> the things you must be careful about.
>
> Read this once from top to bottom. Then keep it open as a map while you explore the code.

---

## 1. What is this system? (In one paragraph)

This is a web application for a trading/distribution company. Salesmen and managers
use it to create **Quotations**, turn them into **Sales Orders**, and track them
through to **Delivery Orders** and **Invoices**. The company runs **SAP** (an ERP system)
as the "source of truth" for real business documents. This web app pulls data **out of
SAP** every few minutes, stores a copy in its own database, and shows nice dashboards,
analysis reports, PDFs, and Excel exports on top of it. It also sends **Telegram** and
**WhatsApp** notifications when important things happen (like an order needing manager
approval).

Think of it as: **"A friendly reporting + order-entry website that sits on top of SAP."**

---

## 2. Technology used (the "stack")

| Layer | Technology | Notes |
|-------|-----------|-------|
| Language | **Python 3.13** | The whole backend is Python |
| Web framework | **Django 5.2.3** | Handles pages, URLs, database, admin |
| API framework | **Django REST Framework** | For the mobile/API endpoints |
| Database | **SQLite** (default) — can switch to **MySQL / PostgreSQL** | Chosen by env variables |
| Frontend | **HTML templates + Bootstrap 5 + Font Awesome** | No React/Vue — plain server-rendered pages with some JavaScript |
| PDF generation | **ReportLab**, **PyPDF2** | For printable documents |
| Excel | **openpyxl**, **pandas** | For imports and exports |
| External systems | **SAP REST API**, **Telegram**, **WhatsApp**, **Supabase S3** | Integrations |

Everything needed is listed in **`requirements.txt`**.

---

## 3. The big picture — how data flows (READ THIS CAREFULLY)

This is the single most important thing to understand. The app runs in **two places**:

```
   ┌─────────────────────────┐         ┌──────────────────────────────────┐
   │   OFFICE PC (on-site)    │         │   VPS / SERVER (on the internet) │
   │                          │         │   salesorder.junaidworld.com     │
   │  Runs the "PC sync       │  HTTPS  │                                  │
   │  scripts" (sync_*_pc.py) │ ──────► │   Runs the Django website        │
   │                          │  push   │   that users actually visit      │
   │  Can reach SAP directly  │  data   │                                  │
   └───────────┬──────────────┘         └──────────────────────────────────┘
               │
               │ local network only
               ▼
   ┌─────────────────────────┐
   │  SAP ERP                 │
   │  http://192.168.1.103    │
   │  /IntegrationApi/api/... │
   └─────────────────────────┘
```

**Why two places?** SAP lives on the company's private local network. A server on the
public internet (the VPS) **cannot reach SAP directly**. So:

1. A **PC inside the office** runs small "sync scripts" (files named `sync_*_pc.py`).
2. Those scripts **read data from SAP** (which the PC can reach).
3. They then **push that data to the VPS** over HTTPS, to special "receive" URLs.
4. The VPS saves it into its database, and users see it on the website.

The shared secret that lets the PC talk to the VPS is **`VPS_API_KEY`** — it must be the
**same value** on both the PC and the VPS, or the VPS will reject the data.

> 📄 A deeper walkthrough of this is in **`docs/SAP_INTEGRATION.md`**. Read it after this.

---

## 4. Full folder & file directory (what each thing is)

### 4.1 Project root (`salesorder-web/salesorder/`)

| Path | What it is |
|------|-----------|
| `manage.py` | Django's command runner. You run everything through this (`python manage.py ...`) |
| `requirements.txt` | List of Python packages to install |
| `.env` | **Secret settings** (DB password, API keys). Not committed to git. See section 7 |
| `db.sqlite3` | The default local database file (if using SQLite) |
| `salesorder/` | The **project config** folder (settings, main URLs). See 4.2 |
| `so/` | The **main app** — sales orders, SAP data, finance, most features. See 5.1 |
| `alabama/` | A **separate division's portal** (its own sales data). See 5.2 |
| `submittal/` | Builds **submittal PDF documents** for projects. See 5.3 |
| `businesscards/` | Digital **business cards** with QR codes for salesmen. See 5.4 |
| `tradelicense/` | Trade-license expiry **reminders** (WhatsApp). See 5.5 |
| `static/` | Shared CSS/JS/images used by the site |
| `media/` | User-uploaded files and generated files (PDFs, cheque images, etc.) |
| `docs/` | Extra deep-dive documents (SAP + approvals). Very useful, read them |
| `logs/` | Log files written by the app and sync scripts |
| `data/`, `data.json` | Sample/seed data and exports |
| `prefect_airflow_demo/` | A separate demo of scheduling tools — **not part of the live app** |

### 4.2 The project config folder — `salesorder/`

This is the "brain" of Django configuration. **It is NOT an app; it wires everything together.**

| File | What it does |
|------|-------------|
| `settings.py` | All configuration: installed apps, database, SAP URLs, Telegram/WhatsApp keys, static/media paths. **Start here to understand config.** |
| `urls.py` | The top-level URL map. Sends `/` to the `so` app, `/alabama/` to alabama, etc. |
| `wsgi.py` / `asgi.py` | The entry points a production web server uses to run Django |

The URL routing at the top level is simple:

```python
urlpatterns = [
    path('admin/', admin.site.urls),          # Django's built-in admin panel
    path('', include('so.urls')),             # Main app = the homepage
    path('alabama/', include('alabama.urls')),
    path('tradelicense/', include('tradelicense.urls')),
    path('businesscards/', include('businesscards.urls')),
    path('submittal/', include('submittal.urls')),
]
```

### 4.3 Root-level sync scripts & helpers

These run **on the office PC**, not on the VPS.

| File | Purpose |
|------|---------|
| `sync_salesorders_pc.py` | Pull Sales Orders from SAP → push to VPS |
| `sync_quotations_pc.py` | Pull Quotations from SAP → push to VPS |
| `sync_purchaseorders_pc.py` | Pull Purchase Orders |
| `sync_arinvoices_pc.py` | Pull AR (customer) Invoices |
| `sync_arcreditmemos_pc.py` | Pull AR Credit Memos |
| `sync_customer_finance_pc.py` | Pull customer finance/outstanding balances |
| `sync_payment_details_pc.py` | Pull payment details |
| `sync_*.bat` | Windows batch files that launch the above scripts (used by Task Scheduler) |
| `sync_*_cron.py` | Variants meant to be scheduled like cron jobs |

Each `_pc.py` script can be run:
- `python sync_salesorders_pc.py --once` → run one time (for testing)
- `python sync_salesorders_pc.py` → keep running and repeat every few minutes

---

## 5. The Django apps explained (this is where the real work is)

A Django "app" is a self-contained feature folder. Inside most apps you'll find the
same standard files, so learn them once:

| File in an app | What it holds |
|----------------|--------------|
| `models.py` | **Database tables** (each `class` = one table) |
| `views.py` (and `*_views.py`) | **The logic** for each page — what happens when a URL is visited |
| `urls.py` | Maps URLs to the view functions in this app |
| `forms.py` | Form definitions and validation |
| `admin.py` | Registers models into the Django admin panel |
| `templates/` | The HTML pages |
| `migrations/` | Auto-generated files that build/change database tables |
| `apps.py`, `tests.py` | App config and tests |

### 5.1 `so/` — THE MAIN APP (most of the system)

This is by far the biggest app (over 100 templates). It contains the core sales flow,
SAP data, finance, analysis, PDFs, devices, and the API.

Because it is large, its code is **split into many view files** by topic instead of one
giant `views.py`:

| View file | Handles |
|-----------|--------|
| `views.py` | Login, home dashboard, customers, items, in-app sales orders, uploads |
| `views_quotation.py` | In-app quotations and converting them to sales orders |
| `sap_salesorder_views.py` | SAP sales orders, invoices, credit memos, proforma invoices |
| `sap_quotation_views.py` | SAP quotations |
| `sap_purchaseorder_views.py` | SAP purchase orders |
| `finance_statement_views.py` | Customer finance statements & credit edits |
| `customer_analysis_views.py` | Customer sales analysis reports |
| `item_sold_analysis_views.py`, `item_quoted_analysis_views.py`, `quotation_item_analysis_views.py` | Item-level analysis reports |
| `credit_memo_analysis_views.py` | Credit-memo analysis |
| `historical_sales_views.py` | Old historical sales data upload + analysis |
| `purchase_stock_requirement_views.py` | What stock needs to be purchased |
| `accounts_recording_views.py` | Accounts / payment recording |
| `*_pdf_export.py` | The PDF-generating versions of the above reports |

**Key support files in `so/`:**

| File | Purpose |
|------|---------|
| `api_client.py` | The code that **calls SAP's REST API** and returns data |
| `sync_services.py` | The code that **saves SAP data into our database** (the "core" sync logic all entry points share) |
| `serializers.py` | Converts models to/from JSON for the REST API |
| `middleware.py`, `device_middleware.py` | Device tracking + restricting which devices can log in |
| `telegram_remarks.py` | Sends Telegram messages |
| `brand_margins_service.py` | Business rules about profit margins per brand |
| `salesman_mapping.py` | Maps login users to salesman names (who sees whose data) |
| `signals.py` | Auto-run code when certain models are saved |
| `forms.py`, `utils.py` | Forms and helper functions |
| `management/commands/` | **Custom `manage.py` commands** (see section 8) |

**Main database tables (`so/models.py`):** These fall into groups —

- **Master data:** `Customer`, `Items`, `Salesman`, `Role`, `CustomerPrice`, `IgnoreList`
- **In-app documents:** `SalesOrder`, `OrderItem`, `Quotation`, `QuotationItem`
- **Copies of SAP documents:** `SAPQuotation(+Item)`, `SAPSalesorder(+Item)`,
  `SAPPurchaseOrder(+Item)`, `SAPProformaInvoice(+Line)`, `SAPARInvoice(+Item)`,
  `SAPARCreditMemo(+Item)`
- **Finance:** `CustomerPendingInvoice`, `FinanceCreditEditLog`, `AccountsRecordingEntry`,
  `AccountsRecordingChangeLog`
- **Devices/security:** `Device`, `TrustedDevice`
- **Other:** `HistoricalSalesLine`, `OpenSalesOrder`, `ProposedQuantity`, logs

> **Naming tip:** Anything starting with `SAP...` is a **copy of data that came from SAP**.
> Anything without that prefix (like `SalesOrder`, `Quotation`) is a document **created
> inside this web app** by a user.

### 5.2 `alabama/` — a separate division's portal (`/alabama/`)

Alabama is treated like a mini-version of the main app for a different business division.
It has **its own sales/purchase data uploaded from Excel** (not synced from SAP the same
way), its own homepage, calendar, and analysis pages.

- **Models (`alabama/models.py`):** `AlabamaSalesLine`, `AlabamaPurchaseLine`,
  `AlabamaSAPQuotation(+Item)`, `AlabamaSalesOrder(+Item)`, `AlabamaDeliveryOrder(+Item)`,
  `AlabamaSalesmanMapping`
- **View files:** `views.py` (home + settings + summaries), `sales_analysis_views.py`,
  `item_analysis_views.py`, `customer_analysis_views.py`, `delivery_order_views.py`,
  `salesorder_views.py`
- **`context_processors.py`** injects Alabama-wide values into every Alabama template
  (registered in `settings.py` under `TEMPLATES`).
- The homepage (`alabama/home.html`) has a sales **calendar with year/month filters and
  weekly totals** — this mirrors the main app's homepage calendar.

### 5.3 `submittal/` — project submittal PDF builder (`/submittal/`)

Builds professional "submittal" packages (a bundle of product datasheets, certifications,
and cover pages) as a single PDF for construction/contracting projects.

- **Models:** `Submittal`, `SubmittalMaterial`, `SubmittalBrand`, `MaterialCertification`,
  `CompanyDocuments`, `SectionDivider`, `ProjectContractorHistory`, `ComplianceOption`,
  `RemarkOption`, `SubmittalSectionUpload`
- **Key files:** `pdf_builder.py` (assembles the final PDF), `services.py` (logic),
  `storage_backends.py` (saves the generated PDF to **Supabase S3** cloud storage)
- Has a **wizard** flow (`/submittal/new/`) and an **admin area** (`/submittal/admin/...`)

### 5.4 `businesscards/` — digital business cards (`/businesscards/`)

Creates a public web page + QR code + downloadable contact file (`.vcf`) for each salesman.

- **Model:** `SalesmanCard`
- **Public pages:** `/businesscards/card/<slug>/` (the shareable card) and
  `/businesscards/vcard/<slug>.vcf` (phone contact download)
- `services.py` generates QR codes (uses the `qrcode` library)

### 5.5 `tradelicense/` — license expiry reminders (`/tradelicense/`)

Upload a list of customers with trade-license expiry dates; the app sends **WhatsApp**
reminder notifications before they expire.

- **Models:** `Customer`, `Notification`
- **Key file:** `whatsapp_utils.py` (sends WhatsApp messages via the WhatsApp Cloud API)

---

## 6. Common features & where they live (quick lookup)

| I want to find… | Look here |
|-----------------|-----------|
| The login page | `so/views.py` → `login_view` |
| The main dashboard/homepage | `so/views.py` → `home` (template `so/templates/so/home.html`) |
| How SAP data is fetched | `so/api_client.py` |
| How SAP data is saved | `so/sync_services.py` |
| Sales order approval + Telegram | `so/sap_salesorder_views.py` + `docs/SALESORDER_APPROVAL_AND_TELEGRAM.md` |
| Finance statements | `so/finance_statement_views.py` |
| Analysis/reports | the various `*_analysis_views.py` files in `so/` |
| PDF exports | the `*_pdf*.py` files |
| The mobile/JSON API | bottom of `so/urls.py` (paths starting with `api/`) |
| Alabama division | the `alabama/` app |

---

## 7. Configuration & secrets (`.env` and `settings.py`)

Configuration comes from **environment variables**, loaded from a file named **`.env`**
in the project root (via `python-dotenv`). This file is **not** in git — you must create
it on each machine.

**Environment variables currently used (`.env`):**

| Variable | Meaning |
|----------|---------|
| `DB_ENGINE` | Which database (`django.db.backends.sqlite3` / `...mysql` / `...postgresql`) |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Database connection |
| `VPS_API_KEY` | Shared secret so the PC sync scripts can push data to the VPS |
| `TELEGRAM_MD_APPROVAL_BOT_TOKEN`, `TELEGRAM_MD_APPROVAL_CHAT_ID` | The "MD Approvals" Telegram bot & group |

**Other important config that lives in `settings.py`:**

- `SAP_API_BASE_HOST` — where SAP is (default `http://192.168.1.103`). On the VPS this is
  set to an SSH tunnel address instead.
- `VPS_BASE_URL` — the public site address (`https://salesorder.junaidworld.com`).
- `SAP_SYNC_DAYS_BACK` — how many days of data each sync pulls (default 3).
- Static files: `STATIC_ROOT = staticfiles/`, `MEDIA_ROOT = media/`.
- Supabase S3 keys (`S3_ACCESS_KEY_ID`, etc.) — only used for generated submittal PDFs.

> ⚠️ **SECURITY — please fix during handover:** Some secrets are currently **hard-coded
> directly in `settings.py`** (a Telegram bot token, Telegram chat IDs, and a WhatsApp
> access token). These should be moved into `.env` like the others, and the old values
> should be rotated (regenerated), because they are exposed in the source code. Also
> `DEBUG = True` and `ALLOWED_HOSTS = ['*']` are set — these are fine for development but
> **must be changed for production** (`DEBUG = False`, and a real host list).

---

## 8. How to run it locally (step by step)

```bash
# 1. Go into the project
cd salesorder-web/salesorder

# 2. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux

# 3. Install the Python packages
pip install -r requirements.txt

# 4. Create your .env file (copy the variables listed in section 7)
#    For a quick local start, SQLite needs no DB settings at all.

# 5. Build the database tables
python manage.py migrate

# 6. Create an admin login for yourself
python manage.py createsuperuser

# 7. Start the development server
python manage.py runserver
```

Then open **http://127.0.0.1:8000** in your browser. The Django admin panel is at
**http://127.0.0.1:8000/admin**.

---

## 9. Useful `manage.py` commands (custom tools)

The main app ships custom commands in `so/management/commands/`. Run any of them with
`python manage.py <name>`. The most useful ones:

**Importing master data (usually from Excel):**
- `import_items`, `import_items2` — load the item/product list
- `import_customers` — load customers
- `import_customer_prices` — load per-customer prices
- `import_ignorelist` — load items to ignore

**Syncing SAP data (the `_api` versions run on the PC; the `_vps` versions run on the server):**
- `sync_salesorders_api` / `sync_salesorders_vps`
- `sync_quotations_api` / `sync_quotations_vps`
- `sync_purchaseorders_api` / `sync_purchaseorders_vps`
- `sync_arinvoices_api` / `sync_arinvoices_vps`
- `sync_arcreditmemos_api` / `sync_arcreditmemos_vps`
- `sync_customer_finance`, `sync_payment_details`
- `sync_all_sales_data`, `sync_all_sales_data_vps` — run everything at once

**Maintenance / fixes:**
- `check_salesorder_margins` — recheck profit margins (may trigger MD-approval flags)
- `update_stock`, `update_so_items_stock` — refresh stock numbers
- `fix_pi_dates`, `fix_pi_status`, `fix_salesman_names`, `update_nfref_from_excel` — one-off cleanups

> Standard Django commands you'll use daily: `migrate`, `makemigrations`,
> `createsuperuser`, `runserver`, `collectstatic`, `shell`.

---

## 10. Going to production (deployment)

The full checklist is in **`PRODUCTION_DEPLOYMENT_CHECKLIST.md`**. In short:

1. Set `DEBUG = False` and a proper `ALLOWED_HOSTS` (via env).
2. Move all secrets into `.env` (see the security note in section 7).
3. Point the database env variables at the production MySQL/PostgreSQL.
4. Run `python manage.py migrate` and `python manage.py collectstatic`.
5. Serve Django with a real server (e.g. **Gunicorn** behind **Nginx**), not `runserver`.
6. On the **office PC**, schedule the `sync_*_pc.py` scripts (via `.bat` files + Windows
   Task Scheduler) so data keeps flowing to the VPS.
7. Make sure `VPS_API_KEY` matches on the PC and the VPS.
8. Performance tips (indexes, batching, incremental sync) are in
   **`PERFORMANCE_OPTIMIZATION.md`**.

---

## 11. Other documents in this repo (read these next)

| File | What it covers |
|------|---------------|
| `README.md` | Short overview + quick start |
| `docs/SAP_INTEGRATION.md` | Deep walkthrough of the SAP sync (api_client → sync_services → models) |
| `docs/SALESORDER_APPROVAL_AND_TELEGRAM.md` | The approval workflow and every Telegram/notification message |
| `PRODUCTION_DEPLOYMENT_CHECKLIST.md` | Step-by-step production deployment |
| `PERFORMANCE_OPTIMIZATION.md` | How to make the sync faster at scale |

---

## 12. Glossary (business terms)

| Term | Meaning |
|------|---------|
| **SAP** | The company's main ERP system; the official source of truth |
| **QU / Quotation** | A price offer given to a customer |
| **SO / Sales Order** | A confirmed order the customer placed |
| **DO / Delivery Order** | The document for physically delivering goods |
| **PI / Proforma Invoice** | A preliminary invoice (often before payment) |
| **AR Invoice** | Accounts-Receivable invoice — the real bill sent to a customer |
| **Credit Memo** | A document that reduces what a customer owes (like a refund/return) |
| **PO / Purchase Order** | An order the company places with its own suppliers |
| **GP / GP%** | Gross Profit and Gross Profit percentage |
| **MD Approval** | Managing Director approval — required when an order's margin is too low |
| **VPS** | Virtual Private Server — the internet server that hosts the website |
| **Salesman scope** | The rule that limits each salesman to seeing only their own customers/data |

---

## 13. Suggested first-week plan for the new developer

1. **Day 1:** Read this document + `README.md`. Get the app running locally (section 8).
   Log into `/admin` and click around the data.
2. **Day 2:** Read `docs/SAP_INTEGRATION.md`. Open `so/api_client.py` and
   `so/sync_services.py` side by side and trace how one sales order comes in from SAP.
3. **Day 3:** Read `docs/SALESORDER_APPROVAL_AND_TELEGRAM.md`. Follow the approval flow
   in `so/sap_salesorder_views.py`.
4. **Day 4:** Explore the homepage (`so/views.py → home`) and one analysis report to see
   how pages, views, and templates connect.
5. **Day 5:** Look at a smaller app end-to-end (`businesscards/` or `alabama/`) — small
   enough to fully understand, and it cements how a Django app is put together.

---

*Prepared as a handover reference. Keep it updated as the project changes.*
