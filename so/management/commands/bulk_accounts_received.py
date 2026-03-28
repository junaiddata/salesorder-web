"""
One-off / bulk: set Accounts Recording to “Received by A/c” with Received back ticked.

By default, lines are matched against SAPARInvoice (and optionally SAPARCreditMemo) so rows
are created/updated even when no Accounts Recording row existed yet (that is why a plain
file match against AccountsRecordingEntry often found almost nothing).

Usage (from folder containing manage.py):
  python manage.py bulk_accounts_received --file invoices.txt --dry-run
  python manage.py bulk_accounts_received --file invoices.txt
  python manage.py bulk_accounts_received --file mixed.txt --credit-memos
  python manage.py bulk_accounts_received --file x.txt --entries-only   # old behaviour

File: one document number per line; empty and # comments skipped.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from so.models import AccountsRecordingEntry, SAPARInvoice, SAPARCreditMemo


def _normalize_token(s):
    """Strip BOM/whitespace; turn Excel-style 12345.0 into 12345."""
    s = (s or "").strip().strip("\ufeff").replace(",", "")
    if not s:
        return s
    try:
        f = float(s)
        if abs(f - round(f)) < 1e-9:
            return str(int(round(f)))
    except ValueError:
        pass
    return s


def _register_lookup(mp, kind, canonical, *keys):
    for k in keys:
        if not k:
            continue
        kl = k.lower() if isinstance(k, str) else k
        for variant in (k, kl):
            if variant not in mp:
                mp[variant] = (kind, canonical)


def _build_sap_lookup(include_credit_memos=False):
    """
    Map many string variants -> (document_kind, canonical_number as stored in SAP / Accounts).
    """
    mp = {}

    def variants_for(num):
        out = {num, num.strip(), num.lower()}
        try:
            out.add(str(int(float(num))))
        except ValueError:
            pass
        return out

    for inv_num in SAPARInvoice.objects.values_list("invoice_number", flat=True).iterator(
        chunk_size=2000
    ):
        kind = AccountsRecordingEntry.DocumentKind.INVOICE
        for v in variants_for(inv_num):
            _register_lookup(mp, kind, inv_num, v)

    if include_credit_memos:
        for cm_num in SAPARCreditMemo.objects.values_list("credit_memo_number", flat=True).iterator(
            chunk_size=2000
        ):
            kind = AccountsRecordingEntry.DocumentKind.CREDIT_MEMO
            for v in variants_for(cm_num):
                _register_lookup(mp, kind, cm_num, v)

    return mp


def _resolve_line(raw, sap_lookup):
    """Return (document_kind, canonical_document_number) or None."""
    tokens = []
    r = raw.strip().strip("\ufeff")
    if r:
        tokens.append(r)
    n = _normalize_token(raw)
    if n and n not in tokens:
        tokens.append(n)
    for t in tokens:
        if t in sap_lookup:
            return sap_lookup[t]
        tl = t.lower()
        if tl in sap_lookup:
            return sap_lookup[tl]
    return None


def _read_lines(path):
    lines = []
    seen = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            raw = line.rstrip("\n\r")
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped not in seen:
                seen.add(stripped)
                lines.append(stripped)
    return lines


class Command(BaseCommand):
    help = (
        "Bulk set Accounts Recording to Received by A/c + received back. "
        "Matches file lines to SAP AR invoices (creates Accounting rows if missing)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            "-f",
            required=True,
            help="UTF-8 text file: one invoice number (or credit memo number with --credit-memos) per line",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report only; do not write to the database",
        )
        parser.add_argument(
            "--credit-memos",
            action="store_true",
            help="Also match SAP credit memo numbers (same file; tries invoice first, then memo)",
        )
        parser.add_argument(
            "--entries-only",
            action="store_true",
            help="Only update existing AccountsRecordingEntry rows (exact document_number match; no SAP lookup)",
        )
        parser.add_argument(
            "--write-missing",
            metavar="PATH",
            help="Write file lines that could not be matched to this path",
        )

    def handle(self, *args, **options):
        file_path = options["file"]
        dry_run = options["dry_run"]
        include_credit_memos = options["credit_memos"]
        entries_only = options["entries_only"]
        missing_path = options.get("write_missing")

        try:
            lines = _read_lines(file_path)
        except OSError as e:
            raise CommandError(f"Cannot read file: {e}") from e

        if not lines:
            raise CommandError("No lines found in file.")

        self.stdout.write(f"Unique lines in file: {len(lines)}")

        target_status = AccountsRecordingEntry.Status.RECEIVED_BY_AC

        if entries_only:
            self._run_entries_only(lines, dry_run, missing_path, target_status)
            return

        self.stdout.write("Loading SAP AR numbers for lookup…")
        sap_lookup = _build_sap_lookup(include_credit_memos=include_credit_memos)

        targets = []
        not_in_sap = []
        seen_targets = set()

        for raw in lines:
            resolved = _resolve_line(raw, sap_lookup)
            if not resolved:
                not_in_sap.append(raw)
                continue
            kind, canonical = resolved
            key = (kind, canonical)
            if key not in seen_targets:
                seen_targets.add(key)
                targets.append(key)

        self.stdout.write(
            f"Resolved to {len(targets)} distinct SAP document(s); "
            f"{len(not_in_sap)} line(s) not found in SAPARInvoice"
            f"{'/SAPARCreditMemo' if include_credit_memos else ''}."
        )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN: would update_or_create {len(targets)} Accounts Recording row(s)."
                )
            )
        else:
            created = 0
            updated = 0
            now = timezone.now()
            for kind, canonical in targets:
                obj, was_created = AccountsRecordingEntry.objects.update_or_create(
                    document_kind=kind,
                    document_number=canonical,
                    defaults={
                        "status": target_status,
                        "received_back": True,
                        "updated_at": now,
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done: {created} created, {updated} updated (Received by A/c, Received back)."
                )
            )

        if missing_path and not_in_sap:
            with open(missing_path, "w", encoding="utf-8") as out:
                for m in not_in_sap:
                    out.write(f"{m}\n")
            self.stdout.write(
                self.style.WARNING(
                    f"Wrote {len(not_in_sap)} unmatched line(s) to {missing_path} "
                    f"(not in SAP DB with current keys)"
                )
            )

    def _run_entries_only(self, lines, dry_run, missing_path, target_status):
        kind = AccountsRecordingEntry.DocumentKind.INVOICE
        existing = set()
        batch_size = 500
        for i in range(0, len(lines), batch_size):
            batch = lines[i : i + batch_size]
            existing.update(
                AccountsRecordingEntry.objects.filter(
                    document_kind=kind,
                    document_number__in=batch,
                ).values_list("document_number", flat=True)
            )
        missing = [n for n in lines if n not in existing]
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN (--entries-only): would update {len(existing)} row(s); "
                    f"{len(missing)} not in AccountsRecordingEntry."
                )
            )
        else:
            total = 0
            for i in range(0, len(lines), batch_size):
                batch = [n for n in lines[i : i + batch_size] if n in existing]
                if not batch:
                    continue
                n = AccountsRecordingEntry.objects.filter(
                    document_kind=kind,
                    document_number__in=batch,
                ).update(
                    status=target_status,
                    received_back=True,
                    updated_at=timezone.now(),
                )
                total += n
            self.stdout.write(self.style.SUCCESS(f"Updated {total} Accounts Recording row(s)."))
            self.stdout.write(f"Not found: {len(missing)}")

        if missing_path and missing:
            with open(missing_path, "w", encoding="utf-8") as out:
                for m in missing:
                    out.write(f"{m}\n")
            self.stdout.write(self.style.WARNING(f"Wrote {len(missing)} line(s) to {missing_path}"))
