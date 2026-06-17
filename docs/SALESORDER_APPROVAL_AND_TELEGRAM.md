# Sales Order Approval System & Telegram Notifications — Complete Walkthrough

This document explains, end to end, how the **sales order approval workflow** works and how
**Telegram notifications** are wired into it. It covers the data model, the approval states,
who can change what, the automatic margin check that flags orders for MD approval, the
Telegram bot/chat setup, every notification that gets sent, and the exact code paths and
URLs involved.

It is written so that a new developer (or future-you) can understand the whole thing without
reading every line of `sap_salesorder_views.py`.

---

## 1. The big picture

A sales order (SO) is pulled from SAP into the local DB as a `SAPSalesorder` row. Every SO
carries an **approval status**. That status starts at `Pending` and moves through a small
state machine driven by two actors:

1. **Humans** — Admin / Manager users change the status from the SO detail page.
2. **A scheduled job** — `check_salesorder_margins` runs on cron, inspects each open+pending
   SO's item margins against a brand-margins API, and auto-flags low-margin orders as
   `MD Approval Required`.

Whenever something meaningful happens (status change, a management remark, a low-margin
flag), a **Telegram message** is fired to the relevant group so the salesman / management
sees it immediately — optionally with the SO PDF attached.

```
            ┌───────────────────────────────────────────────────────────┐
            │                     SAPSalesorder row                       │
            │   approval_status: Pending → … → Approved / Rejected / …    │
            └───────────────┬───────────────────────────┬───────────────┘
                            │                           │
              human action  │                           │  cron job (every few min)
        (SO detail page)    │                           │  check_salesorder_margins
                            ▼                           ▼
            ┌───────────────────────────┐   ┌───────────────────────────────┐
            │ salesorder_update_approval │   │ brand_margins_service          │
            │ salesorder_send_remarks_*  │   │ check_salesorder_margin()      │
            └───────────────┬───────────┘   └───────────────┬───────────────┘
                            │                               │
                            ▼                               ▼
            ┌───────────────────────────┐   ┌───────────────────────────────┐
            │ telegram_remarks.py        │   │ utils.send_md_approval_telegram│
            │  → salesman group          │   │  → "MD Approvals" group        │
            └───────────────┬───────────┘   └───────────────┬───────────────┘
                            │                               │
                            └───────────────┬───────────────┘
                                            ▼
                              Telegram Bot API (sendMessage / sendDocument)
```

---

## 2. The data model

All approval state lives on the `SAPSalesorder` model (`so/models.py`).

### 2.1 Approval status choices

```python
APPROVAL_STATUS_CHOICES = [
    ('Pending', 'Pending'),
    ('Approved', 'Approved'),
    ('Rejected', 'Rejected'),
    ('Scheduled', 'Scheduled'),
    ('SO Closed/Completed', 'SO Closed/Completed'),
    ('Partial DO', 'Partial DO'),
    ('Trade License Expired', 'Trade License Expired'),
    ('MD Approval Required', 'MD Approval Required'),
]
```

### 2.2 Fields on `SAPSalesorder`

| Field | Purpose |
|-------|---------|
| `approval_status` | `CharField(choices=APPROVAL_STATUS_CHOICES, default='Pending')`. The single source of truth for where the SO is in the workflow. |
| `approval_updated_by` | `ForeignKey(User)` — who last changed the status. |
| `approval_updated_at` | `DateTimeField` — when it was last changed. |
| `remarks` | Internal remarks (Admin-only edit, salesman can view). This is the text pushed to Telegram as "Management remarks". |
| `management_remarks` | "PDF remarks" — printed on the exported SO PDF, visible to everyone on the detail page. |
| `salesman_remarks` | Free text the salesman themselves can edit. |

The schema arrived over several migrations (useful history):

- `0089_add_sapsalesorder_approval_status` — adds the field.
- `0090_add_approval_updated_fields` — adds `approval_updated_by` / `approval_updated_at`.
- `0091_add_approval_status_choices` / `0097_alter_sapsalesorder_approval_status` — expand the choice list.
- `0092_add_md_approval_required` — adds the `MD Approval Required` state.
- `0093_add_sap_remarks` — adds SAP-sourced remarks fields.
- `0099_add_scheduled_approval_status` — adds the `Scheduled` state.

---

## 3. The approval states and what they mean

| Status | Set by | Meaning |
|--------|--------|---------|
| **Pending** | default on import | New / not yet reviewed. The only state the margin job will overwrite. |
| **MD Approval Required** | margin cron job (auto) or Admin | An item is below its required brand margin; management must review. |
| **Approved** | **`manager` user only** | Cleared to proceed. |
| **Rejected** | **`manager` user only** | Not allowed to proceed. |
| **Scheduled** | Admin | Deferred / planned for later. |
| **Partial DO** | Admin / SAP sync | Partially delivered. |
| **SO Closed/Completed** | SAP sync (auto) or Admin | Closed in SAP; set automatically when the SAP `status` becomes Closed. |
| **Trade License Expired** | Admin | Blocked because the customer's trade license is expired. |

### 3.1 Permission rules (enforced in `salesorder_update_approval`)

- Only users whose `role.role == 'Admin'` may change approval status at all.
- **`Approved` and `Rejected` are "manager-only"**: even an Admin cannot set them unless their
  `username == 'manager'`. (A no-op "change" to the same value is allowed.) This is the
  `MANAGER_ONLY_STATUSES = ('Approved', 'Rejected')` guard.
- Non-staff Admins are additionally scope-checked via `salesman_scope_q_salesorder(user)` so
  they can only touch SOs in their own salesman scope.
- In the UI (`salesorder_detail.html`), the dropdown hides manager-only statuses for
  non-managers (`MANAGER_ONLY_STATUSES`), except it keeps showing the current value if the SO
  is already in one of those states.

### 3.2 Auto-transitions from SAP sync

During SAP import (`sap_salesorder_views.py`), when an order's SAP `status` is Closed the code
forces `approval_status = 'SO Closed/Completed'` (see lines around 542, 873–875, 932–933).
This keeps the local workflow consistent with SAP without a human touching it.

---

## 4. The automatic margin check (MD Approval Required)

This is the "smart" part of the system: it watches for under-priced orders and escalates them.

### 4.1 Brand margins service — `so/brand_margins_service.py`

- `fetch_brand_margins()` calls the external API:
  ```
  https://stock.junaidworld.com/api/brand-margins
  ```
  It returns `{ manufacturer_name: min_margin_pct }`. Results are cached in-process for
  1 hour (`CACHE_TTL_SECONDS = 3600`). On any network/parse error it returns `{}` and the
  check is skipped silently (fail-safe: never flags on bad data).
- `DEFAULT_MARGIN_PCT = 15.0` is the fallback when a manufacturer isn't in the API (or when
  the `- No Manufacturer -` catch-all is absent).

- `check_salesorder_margin(salesorder, brand_margins)`:
  1. **Guard:** does nothing unless `approval_status == 'Pending'`. It never overrides
     Approved/Rejected/Closed/etc.
  2. Loads the SO's line items and batch-loads each item's `item_cost` + `item_firm` from the
     `Items` master.
  3. For each item computes `unit_price = row_total / quantity`, then
     `margin_pct = (unit_price - cost) / unit_price * 100`.
  4. Looks up the required margin for the item's manufacturer (`item.manufacture`, falling back
     to the master's `item_firm`).
  5. If **any** item's margin is below its required threshold, it sets
     `approval_status = 'MD Approval Required'`, saves, and returns `True`. (One failing item is
     enough — it breaks early.)

- `run_margin_check_for_queryset(qs)` is a helper that runs the check across a queryset of
  Pending SOs and returns the number updated.

### 4.2 The cron command — `so/management/commands/check_salesorder_margins.py`

```bash
python manage.py check_salesorder_margins          # default: Open + Pending SOs
python manage.py check_salesorder_margins --all    # re-check all Open SOs (backfill)
python manage.py check_salesorder_margins --test-telegram   # send a test msg and exit
```

Flow:
1. Fetch brand margins (abort if empty).
2. Build the queryset:
   - default: `status in ('O','OPEN')` **and** `approval_status='Pending'`
   - `--all`: all `status in ('O','OPEN')` (still won't re-notify already-flagged SOs because
     `check_salesorder_margin` guards on `Pending`).
3. For each SO, run `check_salesorder_margin`. If it got flagged, call
   `_send_md_approval_notification(so)` to ping the **MD Approvals** Telegram group.
4. Log a summary (`Checked / Newly flagged / Notifications sent`) to
   `logs/check_margins.log` (rotating, 5 MB × 3).

Recommended cron (from the command's docstring) — runs every 4 minutes on the VPS:

```cron
*/4 * * * * cd /var/www/salesorder-web2/salesorder && \
  /var/www/salesorder-web2/salesorder/venv/bin/python manage.py check_salesorder_margins \
  >> /var/log/sync_check_margins.log 2>&1
```

---

## 5. Telegram setup

### 5.1 Bots, tokens & chat IDs (`salesorder/settings.py`)

```python
TELEGRAM_BOT_TOKEN          = "8282988077:AAFF…GLzZn4"   # main bot (hard-coded)
TELEGRAM_CREATE_CHAT_ID     = "-4928205676"              # "order created" group
TELEGRAM_APPROVE_CHAT_ID    = "-4900133568"              # "order approve" group

# MD-Approvals — kept in the environment (.env), NOT in source:
TELEGRAM_MD_APPROVAL_BOT_TOKEN = os.getenv('TELEGRAM_MD_APPROVAL_BOT_TOKEN', '')
TELEGRAM_MD_APPROVAL_CHAT_ID   = os.getenv('TELEGRAM_MD_APPROVAL_CHAT_ID', '')
```

Two bots are in play:
- **Main bot** (`TELEGRAM_BOT_TOKEN`) — used for legacy "order created / approved" pings and as
  the fallback for everything else.
- **MD Approvals bot** (`TELEGRAM_MD_APPROVAL_BOT_TOKEN`) — a separate bot for the management
  approvals group. **All the SO-detail remark/status notifications and the margin-job
  notifications prefer this token**, falling back to the main token when it isn't set:
  ```python
  token = getattr(settings, 'TELEGRAM_MD_APPROVAL_BOT_TOKEN', '') or settings.TELEGRAM_BOT_TOKEN
  ```

> **Note:** The MD-approval token/chat ID are read from environment variables, so they can be
> rotated without a code change. The main token is currently hard-coded in `settings.py`.

### 5.2 Low-level send helpers (`so/utils.py`)

- `send_telegram_message(chat_id, text, parse_mode="Markdown", token=None)` — POSTs to the
  Bot API `sendMessage`. Returns `(success: bool, error: str|None)` — it never raises, so a
  Telegram outage can't break the request.
- `send_telegram_document(chat_id, file_bytes, filename, caption=None, parse_mode="HTML", token=None)`
  — POSTs to `sendDocument` to attach a PDF (30 s timeout).
- `send_md_approval_telegram(chat_id, text)` — convenience wrapper that always uses the MD
  token (or falls back) and `parse_mode="HTML"`.

All higher-level notifications use `parse_mode="HTML"` and pass everything through
`_escape_html()` so customer names with `&`, `<`, `>` can't break the message.

### 5.3 Salesman → chat mapping (`so/telegram_remarks.py`)

Salesman names are mapped to Telegram chat IDs in `SALESMAN_TELEGRAM_GROUPS`. Several
salesman name variants can point at the same group:

```python
SALESMAN_TELEGRAM_GROUPS = {
    "TESTING": "-5266252930",
    "A.MR.RAFIQ": "-5206945591",
    "A. RAFIQ SHABBIR - RASHID": "-5195382862",
    "B.ANISH DIP": "-5231217364",
    ...
}
```

Because SAP names are messy (`A. RAFIQ` vs `A.RAFIQ`), lookups go through
`_normalize_salesman_name()` (uppercase, collapse whitespace, normalize dots) and
`get_chat_id_for_salesman()` tries a direct hit first, then a normalized comparison against
every key. `can_send_remarks_telegram(so)` simply returns whether a chat ID exists — the UI
uses it to show/hide the "Send to Telegram" buttons.

---

## 6. Every notification that gets sent

### 6.1 Management remark → salesman group
`send_remarks_to_salesman_telegram(salesorder, remark_text)` builds a richly formatted HTML
card with an emoji that reflects the approval status:

```
✅ Management Remark Update
━━━━━━━━━━━━━━━━━━━━━
📄 SO:        <so_number>
🏢 Customer:  <name>
🔖 Code:      <customer_code>
👤 Salesman:  <name>
📅 Date:      <posting_date>
🔗 BP Ref:    <bp_reference_no>
💰 Total (excl. VAT): <document_total> AED
📌 Status:    ✅ Approved
━━━━━━━━━━━━━━━━━━━━━
💬 Remarks:
   <blockquote>…</blockquote>
🕐 <timestamp in Asia/Dubai>
```

Status emojis come from `_get_approval_emoji()`: ✅ approved, ❌ rejected, ⏳ pending,
⏸ on_hold, 🔍 review, 📅 scheduled, 📋 default.

### 6.2 Management remark **+ PDF** → salesman group
`send_remarks_with_pdf_to_salesman_telegram(...)` does the same but:
- Renders the SO PDF via `generate_sap_salesorder_pdf_bytes(salesorder)` (same design as the
  "Export" button).
- Sends it with `send_telegram_document`, using the card text as the **caption**.
- Telegram caps captions at **1024 chars**, so if the full card is too long it rebuilds a
  shortened caption (drops optional fields, truncates remarks to 600 chars).
- Filename: `SO_<so_number>_<YYYYMMDD>.pdf`.

### 6.3 Approval status change → salesman group
`send_approval_status_change_telegram(salesorder, old_status, new_status, changed_by)` fires
whenever the status actually changes. It shows the transition and who did it:

```
✅ Approval Status Changed
━━━━━━━━━━━━━━━━━━━━━
📄 SO:       <so_number>
🏢 Customer: <name>
💰 Total (excl. VAT): <document_total> AED
🔄 Status:   Pending  ➜  Approved ✅
👤 Changed by: <user full name / username>
💬 Management remarks:  <blockquote>…</blockquote>   (if any; truncated at 3500 chars)
🕐 <timestamp>
```

### 6.4 Low-margin flag → "MD Approvals" group
`_send_md_approval_notification(so)` in the cron command sends a compact alert with a deep
link back to the SO:

```
🔴 MD Approval Required
SO: <so_number>
Customer: <name>
Salesman: <name>
Date: <posting_date>
https://salesorder.junaidworld.com/sapsalesorders/<so_number>/
```

It targets `TELEGRAM_MD_APPROVAL_CHAT_ID`. If Telegram replies "chat not found"/"supergroup",
it automatically retries with the `-100…` supergroup-ID format. The base URL comes from
`VPS_BASE_URL` (defaults to `https://salesorder.junaidworld.com`).

### 6.5 Legacy create/approve pings (`so/views.py`)
The older local order flow still sends plain pings to the create/approve groups via the main
bot:
- on order create → `send_telegram_message(settings.TELEGRAM_CREATE_CHAT_ID, msg)` (lines ~324, ~5418)
- on approve → `send_telegram_message(settings.TELEGRAM_APPROVE_CHAT_ID, msg)` (line ~1126)

---

## 7. Endpoints & UI wiring

URLs (`so/urls.py`) — all are POST and `@login_required`:

| URL name | Path | View | What it does |
|----------|------|------|--------------|
| `salesorder_update_approval` | `sapsalesorders/<so_number>/approval/` | `salesorder_update_approval` | Validates + sets `approval_status`, stamps `approval_updated_by/at`, optionally saves `remarks`, then fires **6.3** if the status changed. |
| `salesorder_update_remarks` | `…/remarks/` | `salesorder_update_remarks` | Saves internal `remarks`. |
| `salesorder_update_management_remarks` | `…/management-remarks/` | `salesorder_update_management_remarks` | Saves PDF `management_remarks` (Admin/Manager only). |
| `salesorder_update_salesman_remarks` | `…/salesman-remarks/` | `salesorder_update_salesman_remarks` | Saves `salesman_remarks` (salesman or Admin). |
| `salesorder_send_remarks_telegram` | `…/send-remarks-telegram/` | `salesorder_send_remarks_telegram` | Saves remark, sends **6.1**. Returns JSON. |
| `salesorder_send_remarks_telegram_pdf` | `…/send-remarks-telegram-pdf/` | `salesorder_send_remarks_telegram_pdf` | Saves remark, sends **6.2** (with PDF). Returns JSON. |

In `salesorder_detail.html`:
- The approval `<form>` posts to `salesorder_update_approval`. An `onsubmit` hook copies the
  remarks textarea into a hidden `approval-sync-remarks` input so the status change and the
  remark are saved together.
- The two "Send to Telegram" buttons (`send-remarks-telegram-btn`,
  `send-remarks-telegram-pdf-btn`) `fetch()` the JSON endpoints and show a toast on success.
- These buttons only render when `can_send_telegram` (i.e. the salesman has a mapped group) is
  true — passed in from the detail view context.

The Alabama app mirrors a subset of this (`alabama/salesorder_views.py`,
`alabama/telegram_remarks.py`, `alabama/urls.py`) for its own SOs.

---

## 8. End-to-end examples

**A. Manager approves an order**
1. Manager opens the SO detail page, picks `Approved`, optionally types a remark, submits.
2. `salesorder_update_approval` checks role + `username == 'manager'`, saves
   `approval_status='Approved'`, `approval_updated_by`, `approval_updated_at` (and `remarks`).
3. Because `old_status != new_status`, `send_approval_status_change_telegram` posts the
   "Approval Status Changed" card to the salesman's group via the MD bot.
4. A Django success message is shown; a Telegram failure is only logged (never blocks the save).

**B. Cron catches an under-priced order**
1. `*/4` cron runs `check_salesorder_margins`.
2. Brand margins are fetched/cached; each Open+Pending SO is checked.
3. An item is below margin → SO flipped to `MD Approval Required`.
4. `_send_md_approval_notification` posts the 🔴 alert (with deep link) to the MD Approvals
   group. Management opens the link and, as `manager`, sets `Approved`/`Rejected` (example A).

**C. Salesman gets the order PDF on Telegram**
1. Admin types a management remark and clicks "Send to Telegram (PDF)".
2. `salesorder_send_remarks_telegram_pdf` saves the remark, renders the SO PDF, and sends it as
   a document with the formatted caption to the salesman's group. Returns `{success: true}`.

---

## 9. Operational notes / gotchas

- **Fail-safe by design:** every Telegram call returns `(ok, err)` and is only logged on
  failure — notifications never break the web request or the cron job.
- **No double-notify on margins:** the `Pending`-only guard in `check_salesorder_margin` means
  re-running the job (or `--all`) won't spam already-flagged SOs.
- **Name matching is fuzzy on purpose:** if a salesman isn't receiving messages, check that
  their exact SAP `salesman_name` normalizes to a key in `SALESMAN_TELEGRAM_GROUPS`.
- **Supergroup IDs:** if a group was upgraded to a supergroup, its chat ID needs the `-100…`
  prefix. The margin notifier retries this automatically; the per-salesman map does not, so
  store the correct form there.
- **Test the pipe quickly:** `python manage.py check_salesorder_margins --test-telegram` sends
  a one-off message to the MD Approvals group and reports which chat-ID format worked.
- **Secrets:** rotate `TELEGRAM_MD_APPROVAL_BOT_TOKEN` / `TELEGRAM_MD_APPROVAL_CHAT_ID` via
  `.env`. The main `TELEGRAM_BOT_TOKEN` is currently hard-coded in `settings.py` and should be
  moved to the environment if this doc prompts a cleanup.

---

## 10. File reference

| File | Responsibility |
|------|----------------|
| `so/models.py` | `SAPSalesorder` + `APPROVAL_STATUS_CHOICES` and approval fields. |
| `so/sap_salesorder_views.py` | Approval / remark endpoints; triggers Telegram on change. |
| `so/telegram_remarks.py` | Salesman→chat map + the 3 formatted salesman notifications. |
| `so/utils.py` | Low-level `send_telegram_message` / `send_telegram_document` / `send_md_approval_telegram`. |
| `so/brand_margins_service.py` | Brand-margins API client + `check_salesorder_margin`. |
| `so/management/commands/check_salesorder_margins.py` | Cron job that flags low-margin SOs and pings MD Approvals. |
| `so/urls.py` | Routes for the approval/remark endpoints. |
| `so/templates/salesorders/salesorder_detail.html` | UI: status dropdown + remark forms + Telegram buttons. |
| `salesorder/settings.py` | Telegram tokens & chat IDs. |
| `alabama/*` | Parallel (lighter) implementation for the Alabama app. |
