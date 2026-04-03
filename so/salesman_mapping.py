"""
Salesman name mapping from SAP format to simplified names.

Also used for combined quotations: canonical labels (e.g. MUZAIN) group SAP spellings
such as B.MR.MUZAIN — see get_quotation_salesman_groups().

Related: so.views.SALES_USER_MAP (username -> allowed name variants for scoping).
"""
from collections import defaultdict


# Mapping from SAP salesman names to simplified names
SALESMAN_MAPPING = {
    "D.RETAIL CUST DIP": "DIP",
    "B. MR.RAFIQ ABU- PROJ": "ABU BAQAR",
    "-No Sales Employee-": "DIP",
    "A.MR.RASHID": "RASHID",
    "A. RAFIQ SHABBIR - RASHID": "RAFIQ",
    "A. RAFIQ ABU - RASHID": "ABU BAQAR",
    "B.MR.NASHEER AHMAD": "NASHEER",
    "NASHEER SIR": "NASHEER",
    "B.MR.MUZAIN": "MUZAIN",
    "A.MR.RAFIQ ABU-TRD": "ABU BAQAR",
    "A.MR.RAFIQ AD": "RAFIQ",
    "A.MR.SIYAB": "SIYAB",
    "A.MR.RAFIQ": "RAFIQ",
    "B.MR.PARTHIBAN": "PARTHIBAN",
    "B.MR.JUNAID": "JUNAID",
    "Z.ONLINE SALES": "DIP",
    "B.ANISH DIP": "ANISH",
    "A.KRISHNAN": "KRISHNAN",
    "A.MR.SIYAB CONT": "SIYAB",
    "A.DIP MUZAMMIL": "MUZAMMIL",
    "A.MR.RASHID CONT": "RASHID",
    "A.DIP ADIL": "DIP",
    "PALSON": "DIP",
}


def map_salesman_name(sap_name, strict=True):
    """
    Map SAP salesman name to simplified name
    
    Args:
        sap_name: Salesman name from SAP API (e.g., "A.MR.RASHID")
        strict: If True, return None for unmapped names. If False, return original name.
    
    Returns:
        Simplified name (e.g., "RASHID") or None if no mapping found (when strict=True)
    """
    if not sap_name:
        return None
    
    sap_name = sap_name.strip()
    
    # Check exact match first
    if sap_name in SALESMAN_MAPPING:
        return SALESMAN_MAPPING[sap_name]
    
    # Check if it starts with any mapped key (for variations)
    for sap_key, mapped_name in SALESMAN_MAPPING.items():
        if sap_name.startswith(sap_key) or sap_key in sap_name:
            return mapped_name
    
    # Return None if strict mode (for filtering), or original if not strict
    if strict:
        return None  # No mapping found, should be ignored
    else:
        return sap_name  # Return original for backward compatibility


def _merge_cluster_into_groups(groups, cluster):
    """Merge a set of equivalent names (e.g. from SALES_USER_MAP) under one canonical key."""
    canon = None
    for n in cluster:
        if n in SALESMAN_MAPPING:
            canon = SALESMAN_MAPPING[n]
            break
    if canon is None:
        for n in cluster:
            for c, variants in groups.items():
                if n in variants:
                    canon = c
                    break
            if canon is not None:
                break
    if canon is None:
        canon = min(cluster, key=len)
    for n in cluster:
        groups[canon].add(n)
    groups[canon].add(canon)


def _place_salesman_name_in_groups(groups, n):
    """Attach a Salesman row or SAP distinct name to an existing group or create a singleton."""
    if n in SALESMAN_MAPPING:
        c = SALESMAN_MAPPING[n]
        groups[c].add(n)
        return
    for canon, variants in groups.items():
        if n in variants:
            return
    groups[n].add(n)


def get_quotation_salesman_groups():
    """
    Canonical display label -> frozenset of all salesman_name strings to match (app + SAP).
    Built from SALESMAN_MAPPING, SALES_USER_MAP, Salesman, and distinct SAPQuotation.salesman_name.
    """
    from so.models import Salesman, SAPQuotation
    from so.views import SALES_USER_MAP

    groups = defaultdict(set)

    for sap_key, canon in SALESMAN_MAPPING.items():
        sk = (sap_key or '').strip()
        c = (canon or '').strip()
        if not c:
            continue
        if sk:
            groups[c].add(sk)
        groups[c].add(c)

    for names in SALES_USER_MAP.values():
        cluster = {(n or '').strip() for n in names if (n or '').strip()}
        if cluster:
            _merge_cluster_into_groups(groups, cluster)

    for row in Salesman.objects.values_list('salesman_name', flat=True).iterator():
        n = (row or '').strip()
        if n:
            _place_salesman_name_in_groups(groups, n)

    sap_qs = (
        SAPQuotation.objects.exclude(salesman_name__isnull=True)
        .exclude(salesman_name='')
        .values_list('salesman_name', flat=True)
        .distinct()
    )
    for name in sap_qs.iterator():
        n = (name or '').strip()
        if n:
            _place_salesman_name_in_groups(groups, n)

    return {k: frozenset(v) for k, v in groups.items() if v}


def get_quotation_salesman_canonical_choices_sorted():
    """Sorted canonical labels for combined-quotations multiselect."""
    return sorted(get_quotation_salesman_groups().keys(), key=lambda x: (x.casefold(), x))


def expand_quotation_salesman_picks(picks):
    """Expand multiselect values (canonical or any known alias) to all spellings for OR / __iexact."""
    if not picks:
        return frozenset()
    groups = get_quotation_salesman_groups()
    out = set()
    for p in picks:
        p = (p or '').strip()
        if not p:
            continue
        if p in groups:
            out.update(groups[p])
            continue
        matched = False
        for canon, variants in groups.items():
            if p == canon or p in variants:
                out.update(variants)
                matched = True
                break
        if not matched:
            out.add(p)
    return frozenset(out)


def normalize_quotation_salesman_picks_to_canonicals(picks):
    """Map GET values to canonical labels so checkboxes stay checked (legacy SAP spellings, etc.)."""
    if not picks:
        return []
    groups = get_quotation_salesman_groups()
    labels = set()
    for p in picks:
        p = (p or '').strip()
        if not p:
            continue
        if p in groups:
            labels.add(p)
            continue
        for canon, variants in groups.items():
            if p == canon or p in variants:
                labels.add(canon)
                break
    return sorted(labels, key=lambda x: (x.casefold(), x))
