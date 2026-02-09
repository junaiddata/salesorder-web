"""
Salesman name mapping from SAP format to simplified names
"""
# Mapping from SAP salesman names to simplified names
SALESMAN_MAPPING = {
    "D.RETAIL CUST DIP": "DIP",
    "B. MR.RAFIQ ABU- PROJ": "ABU BAQAR",
    "-No Sales Employee-": "DIP",
    "A.MR.RASHID": "RASHID",
    "B.MR.NASHEER AHMAD": "NASHEER",
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
