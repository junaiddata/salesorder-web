# SAP Integration — Complete Walkthrough

This document explains how the application pulls data out of SAP Business One and lands
it in the local Django database, end to end. It covers the network topology, the code
path, the data model, and the three different ways a sync can be triggered.

---

## 1. The big picture

SAP Business One exposes a custom **Integration API** (a thin REST wrapper over the SAP
Service Layer / DI) at:

```
http://192.168.1.103/IntegrationApi/api/<Entity>
```

That machine (`192.168.1.103`) lives on the **office LAN**. The Django app runs on a
**VPS** that cannot reach that LAN directly. So there are two deployment realities, and
the code supports both:

```
                         ┌─────────────────────────────────────────────┐
                         │                  SAP B1 host                  │
                         │           192.168.1.103 (office LAN)          │
                         │   /IntegrationApi/api/SalesOrder, etc.        │
                         └───────────────────┬─────────────────────────┘
                                             │  HTTP POST (JSON)
            ┌────────────────────────────────┼────────────────────────────────┐
            │                                 │                                 │
   ┌────────▼─────────┐              ┌────────▼─────────┐
   │  PATH A: PC pull  │              │ PATH B: VPS pull │
   │  + push to VPS    │              │ via SSH tunnel   │
   │ sync_*_pc.py      │              │ manage.py *_vps  │
   └────────┬─────────┘              └────────┬─────────┘
            │ HTTPS POST mapped JSON          │ writes straight to DB
            │ to /…/sync-api-receive/         │
   ┌────────▼──────────────────────────────────▼────────┐
   │                  Django app (VPS)                    │
   │   SAPAPIClient → map → sync_services → DB models     │
   └─────────────────────────────────────────────────────┘
```

The same mapping/persistence logic is reused across both paths. The only difference is
**who calls the SAP API** and **how the mapped data reaches the database**.

---

## 2. Configuration

All endpoints are built from one base host in `salesorder/settings.py:167`:

```python
SAP_API_BASE_HOST = os.getenv('SAP_API_BASE_HOST', 'http://192.168.1.103')
SAP_API_BASE_URL          = f"{SAP_API_BASE_HOST}/IntegrationApi/api/SalesOrder"
SAP_QUOTATION_API_URL     = f"{SAP_API_BASE_HOST}/IntegrationApi/api/SalesQuotations"
SAP_PURCHASE_ORDER_API_URL= f"{SAP_API_BASE_HOST}/IntegrationApi/api/PurchaseOrder"
SAP_AR_INVOICE_API_URL    = f"{SAP_API_BASE_HOST}/IntegrationApi/api/ARInvoice"
SAP_AR_CREDIT_MEMO_API_URL= f"{SAP_API_BASE_HOST}/IntegrationApi/api/ARCreditMemo"
SAP_FINANCE_SUMMARY_API_URL    = f"{SAP_API_BASE_HOST}/IntegrationApi/api/FinanceSummary"
SAP_GET_PAYMENT_DETAILS_API_URL= f"{SAP_API_BASE_HOST}/IntegrationApi/api/GetPaymentDetails"
SAP_API_TIMEOUT = 30
```

Key idea: **switch environments by setting one env var.**

- **On the PC** (office LAN): leave the default → talks directly to `192.168.1.103`.
- **On the VPS**: open an SSH reverse tunnel and set
  `SAP_API_BASE_HOST=http://localhost:8443`. The tunnel forwards `localhost:8443` on the
  VPS back to `192.168.1.103:80` on the LAN:

  ```
  ssh -N -R 8443:192.168.1.103:80 root@VPS
  ```

  That exact command is even printed in the error messages when a connection fails
  (`api_client.py:71`).

`VPS_API_KEY` is a shared secret used only by **Path A** (PC→VPS HTTP push) to
authenticate the receiving endpoints.

---

## 3. The API client — `so/api_client.py`

`SAPAPIClient` is the single class that knows how to talk to SAP. Everything else builds
on it. It has three responsibilities:

### 3a. Talking to the API (transport + pagination)

- `_make_request(payload, page_number)` — POSTs a JSON filter to the **Sales Order**
  endpoint. SAP responds with OData-style `{ "value": [...], "odata.count": N }`. The
  method normalizes that to `{'value': [...], 'count': N}` and translates network errors
  into friendly `RuntimeError`s (timeout / tunnel-down hints). See `api_client.py:26`.
- `_make_request_with_url(payload, base_url, page_number)` — same thing but takes an
  explicit URL, so it can be reused for PO / AR Invoice / Credit Memo endpoints.
  See `api_client.py:947`.
- `_fetch_all_pages(...)` / `_fetch_all_pages_with_url(...)` — SAP returns **20 records
  per page**. These helpers read page 1, compute total pages from `count`, then loop the
  rest. See `api_client.py:79` and `:905`.

### 3b. Fetch methods (what to ask SAP for)

Each entity has a family of fetchers built on the helpers above. For sales orders:

| Method | SAP filter payload | Purpose |
|---|---|---|
| `fetch_open_salesorders()` | `{"DocumentStatus": "bost_Open"}` | all currently open SOs |
| `fetch_salesorders_by_date(d)` | `{"DocDate": d}` | one day |
| `fetch_salesorders_by_docnum(n)` | `{"DocNum": n}` | a single order |
| `fetch_last_n_days(days)` | loops dates | recent activity, deduped by `DocNum` |
| `sync_all_salesorders(days_back)` | open + last N days | the default full sync |

`sync_all_salesorders` (`api_client.py:193`) is the workhorse: it fetches all open
orders, then the last N days of new orders, **dedupes by `DocNum`**, then preloads
manufacturer info for every line item (see caching below). This "open + recent days"
strategy is what catches both newly created orders and changes to existing ones, while
also detecting orders that have since closed (handled later in `sync_services`).

The other entities mirror this with their own payloads:
- **Purchase Orders** use `{"FromDate", "ToDate"}` ranges (`:596`).
- **AR Invoices / Credit Memos** use `{"FromDate", "ToDate"}` (`:738`, `:854`), plus a
  special `fetch_arinvoices_by_cancel_status(...)` for backfilling cancellations (`:789`).

### 3c. Mapping SAP JSON → model dicts

This is the most important part to understand, because the SAP payload is messy and the
mapping encodes a lot of business rules. `_map_api_response_to_model(api_order)`
(`api_client.py:393`) turns one raw SAP order into a flat dict ready for the DB. Highlights:

- **Header extraction**: `DocNum`→`so_number`, `DocEntry`→`internal_number`,
  `DocDate`→`posting_date` (tries `YYYY-MM-DD` then `YYYY/MM/DD`).
- **Business partner**: `BusinessPartner.CardCode/CardName`, `FederalTaxID`→`vat_number`,
  `Phone1`→`customer_phone`. Top-level `Address`→`customer_address`.
- **Remarks**: `ClosingRemarks`→`closing_remarks`, `Comments`→`sap_remarks`
  (carriage returns normalized to newlines).
- **Proforma flag**: `U_PROFORMAINVOICE == 'Y'`→`is_sap_pi`, `U_Lpdate`→`sap_pi_lpo_date`.
  This drives automatic SAP-PI creation later.
- **Quotation reference**: `TaxExtension.NFRef`→`nf_ref` (later parsed to recover the
  source quotation number — see `SAPSalesorder.extract_quotation_number`).
- **Status**: `DocumentStatus == "bost_Open"` → `"O"`, else `"C"`. Same logic per line
  via `LineStatus`.
- **Line items**: `DocumentLines` → list of dicts. `LineNum` is 0-based in SAP and
  converted to 1-based `line_no`. **`pending_amount = RemainingOpenQuantity * Price`** is
  the key derived figure; `document_total` for the header is the sum of those pending
  amounts (falling back to `DocTotal` when everything is closed).

Other mappers follow the same shape with entity-specific rules:
- `_map_purchaseorder_api_response` (`:630`)
- `_map_arinvoice_api_response` (`:1076`) — note the **cancellation sign-flip**: when
  `CancelStatus == 'csCancellation'`, every amount is multiplied by `-1` so a cancelled
  invoice nets out the original. It also computes gross profit per line
  (`line_total_after_discount - cost_price`).
- `_map_arcreditmemo_api_response` (`:1260`)

### 3d. Caching & item auto-creation

- `_load_manufacturer_cache` / `_load_stock_cache` batch-load from the local `Items`
  table to avoid N+1 queries during mapping (`:261`, `:291`). Manufacturer (`item_firm`)
  is denormalized onto each SO line.
- `_ensure_item_exists` (`:1000`) — for AR invoices, if a line references an item code not
  yet in the local `Items` table, it auto-creates a stub `Items` row and removes that code
  from the `IgnoreList`. This keeps foreign keys valid.

---

## 4. The persistence layer — `so/sync_services.py`

The client produces dicts; `sync_services.py` writes them to the DB. There is one
`sync_<entity>_core(...)` function per entity, each returning a `sync_stats` dict
(`created / updated / closed / total_items / api_calls / errors`). They share a common
pattern:

1. **Choose what to fetch** based on args (`docnum`, `from_date/to_date`, `specific_date`,
   or default `days_back`).
2. **Map** every raw order via the client's `_map_*` method, skipping/logging failures.
3. **Upsert inside a `transaction.atomic()` block**:
   - `in_bulk(...)` the existing rows keyed by document number.
   - Split into `to_create` / `to_update`, then `bulk_create` / `bulk_update`.
4. **Reconcile line items.**
5. **Close orphans** — orders that were open locally but no longer appear in the SAP
   response are marked closed.

`sync_salesorders_core` (`sync_services.py:229`) is the canonical example and has a few
extra behaviors worth knowing:

- **Item upsert preserves user edits.** Instead of delete-and-recreate, `upsert_salesorder_items`
  (`:57`) matches rows by `(salesorder_id, line_no)`, updates in place, creates new lines,
  and deletes removed ones. This is deliberate — `revised_price` is a user-entered field
  and must survive a sync. `snapshot_revised_prices_by_so` / `restore_...` (`:31`, `:47`)
  exist as a safety net for the few code paths that still rebuild items.
- **Closing logic** (`:386`): any locally-open SO whose `so_number` is *not* in this
  sync's `api_so_numbers` set is flipped to `status='C'`,
  `approval_status='SO Closed/Completed'`, and its lines zeroed out.
- **Automatic SAP Proforma Invoices** (`:404`): when `is_sap_pi` is true, it
  creates/updates a `SAPProformaInvoice` (number == the SO number) and rebuilds its lines
  from the SO items.
- **Customer upsert** (`:468`): refreshes the `Customer` table with address / phone / VAT
  pulled from the order.
- **Per-entity rotating file logs**: `_get_sync_logger('salesorders')` writes to
  `logs/sync_salesorders.log` (`:185`).

The AR-invoice/credit-memo cores additionally handle the cancellation sign-flip already
applied in mapping, and `sync_cancellation_invoices_core` (`:1133`) is a maintenance tool
to backfill historical cancellations by page range.

---

## 5. How a sync gets triggered (three entry points)

All three ultimately run the same `sync_*_core` logic — they differ only in *where the
SAP call happens* and *how data reaches the VPS DB*.

### Path A — PC pulls, pushes to VPS over HTTPS (`sync_salesorders_pc.py`)

Runs on a PC **on the office LAN** (so it can reach `192.168.1.103` directly). Flow:

1. Use `SAPAPIClient` to fetch open + last-N-days orders and **map** them locally
   (`sync_salesorders_pc.py:114`).
2. Serialize (dates → ISO strings) and `POST` the mapped orders to
   `https://salesorder.junaidworld.com/sapsalesorders/sync-api-receive/` with the shared
   `VPS_API_KEY` (`:184`).
3. Runs either `--once` or as a background service every 7 minutes (uses the `schedule`
   library, `:378`).

The **receiving end** is `sync_salesorders_api_receive` in
`so/sap_salesorder_views.py:760`: it checks the API key, then runs essentially the same
upsert/close logic as the core function, but on the *already-mapped* payload (no SAP call
needed on the VPS). Each entity has its own receive endpoint, wired in `so/urls.py`:
- `sapsalesorders/sync-api-receive/`
- `sapquotations/sync-api-receive/`
- `sappurchaseorders/sync-api-receive/`
- `saparinvoices/sync-api-receive/`
- `saparcreditmemos/sync-api-receive/`

There are matching `manage.py sync_*_api` commands that do the same PC-side fetch+push from
within Django.

### Path B — VPS pulls directly via SSH tunnel (`manage.py sync_*_vps`)

Runs **on the VPS** with the tunnel up and `SAP_API_BASE_HOST=http://localhost:8443`.
`sync_salesorders_vps.py` is a thin wrapper that just calls `sync_salesorders_core(...)`
directly (`sync_salesorders_vps.py:72`). No HTTP hop, no API key — it reads SAP through
the tunnel and writes straight to the DB. These are what you'd put on a VPS cron.

```
python manage.py sync_salesorders_vps
python manage.py sync_salesorders_vps --days-back 7
python manage.py sync_salesorders_vps --specific-date 2026-01-21
python manage.py sync_salesorders_vps --docnum 12345
```

### Path C — Manual sync from the Settings page (web UI)

`so/views.py:5429` (`sync_settings`) renders a page with a dropdown + date/days-back form.
The POST handler `sync_settings_form` (`:5439`) maps the chosen entity to its
`sync_*_core` function and calls it directly (same as Path B, just user-triggered). This
requires the VPS tunnel to be up because it does live SAP calls. Results are surfaced as
Django flash messages.

---

## 6. The data model (where it lands)

Defined in `so/models.py`:

- **`SAPSalesorder`** (`:462`) — one row per SO. Keyed by unique `so_number`. Holds header
  fields plus app-only fields that syncs must not clobber: `remarks`,
  `management_remarks`, `salesman_remarks`, `approval_status`, etc. Indexed on
  `posting_date`, `salesman_name`, `customer_name`, `status`, `nf_ref`.
  `extract_quotation_number()` parses `nf_ref` to recover the source quotation.
- **`SAPSalesorderItem`** (`:537`) — one row per line, FK to the SO, **unique on
  `(salesorder, line_no)`** (that constraint is what makes the in-place upsert possible).
  `revised_price` is the user-editable field preserved across syncs.
- **`SAPProformaInvoice` / `SAPProformaInvoiceLine`** (`:635`, `:669`) — auto-generated
  when `is_sap_pi` is set.
- **`SAPPurchaseOrder`**, `SAPARInvoice`, `SAPARCreditMemo`, `SAPQuotation` and their
  item children mirror the same pattern.
- **`Items`** — local item master used for manufacturer/stock lookups and auto-created
  by `_ensure_item_exists`.

---

## 7. Reading order (suggested)

If you want to trace one full sync in code, read in this order:

1. `salesorder/settings.py:167` — endpoints & how the env switches.
2. `so/api_client.py:193` `sync_all_salesorders` — what gets fetched.
3. `so/api_client.py:393` `_map_api_response_to_model` — the SAP→dict translation rules.
4. `so/sync_services.py:229` `sync_salesorders_core` — upsert, close, PI creation.
5. `so/sync_services.py:57` `upsert_salesorder_items` — why edits survive.
6. Pick your trigger: `sync_salesorders_vps.py` (B), `sync_salesorders_pc.py` +
   `sap_salesorder_views.py:760` (A), or `views.py:5439` (C).

---

## 8. Operational notes / gotchas

- **"Cannot connect to SAP API"** almost always means the SSH tunnel is down on the VPS.
  The exact tunnel command is in `api_client.py:71`.
- **Pagination is fixed at 20/page** by the SAP API; large date ranges = many calls.
- **Closing is inference-based**: an order is closed locally only because it stopped
  appearing in the open/recent response. A too-narrow `days_back` won't *wrongly* close
  things (closing only looks at currently-open local rows vs. the full response set), but
  understand that closure detection relies on the open-orders fetch being complete.
- **Cancellation invoices** carry negative amounts by design (sign-flip in mapping).
- **Two code copies of the upsert exist**: `sync_*_core` (Paths B/C) and the
  `*_api_receive` views (Path A). If you change persistence rules, update both.
