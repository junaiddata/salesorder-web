"""
SAP API Client for fetching Sales Orders
"""
import requests
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from django.conf import settings
from so.models import Items, IgnoreList

logger = logging.getLogger(__name__)


class SAPAPIClient:
    """Client for interacting with SAP Sales Order API"""
    
    def __init__(self):
        self.base_url = getattr(settings, 'SAP_API_BASE_URL', 'http://192.168.1.103/IntegrationApi/api/SalesOrder')
        self.timeout = getattr(settings, 'SAP_API_TIMEOUT', 30)
        # Cache for manufacturer lookups (item_code -> manufacturer)
        self._manufacturer_cache = {}
        self._manufacturer_cache_loaded = False
        # Cache for stock lookups (item_code -> {'total_available_stock': ..., 'dip_warehouse_stock': ...})
        self._stock_cache = {}
    
    def _make_request(self, payload: Dict[str, Any], page_number: int = 1) -> Optional[Dict]:
        """
        Make POST request to SAP API with pagination support
        
        Args:
            payload: Request payload (e.g., {"DocDate": "2026-01-21"} or {"DocumentStatus": "bost_Open"})
            page_number: Page number to fetch (default: 1)
        
        Returns:
            Dictionary with 'value' (list of orders), 'count' (total count), or None if error
        """
        try:
            # Add pageNumber to payload
            request_payload = payload.copy()
            if page_number > 1:
                request_payload['pageNumber'] = page_number
            
            response = requests.post(
                self.base_url,
                json=request_payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            
            # API returns data in 'value' key with 'odata.count' for total
            if isinstance(data, dict):
                return {
                    'value': data.get('value', []),
                    'count': int(data.get('odata.count', 0)) if data.get('odata.count') else 0
                }
            elif isinstance(data, list):
                return {
                    'value': data,
                    'count': len(data)
                }
            else:
                logger.warning(f"Unexpected API response format: {data}")
                return {'value': [], 'count': 0}
                
        except requests.exceptions.Timeout:
            logger.error(f"API request timeout after {self.timeout}s: {payload}, page: {page_number}")
            raise RuntimeError(f"SAP API timeout ({self.timeout}s). Check if SSH tunnel is connected.") from None
        except requests.exceptions.ConnectionError as e:
            logger.error(f"API connection error: {e}, payload: {payload}, page: {page_number}")
            raise RuntimeError("Cannot connect to SAP API. Is the SSH tunnel running? (ssh -N -R 8443:192.168.1.103:80 root@VPS)") from None
        except requests.exceptions.RequestException as e:
            logger.error(f"API request error: {e}, payload: {payload}, page: {page_number}")
            raise RuntimeError(f"SAP API error: {e}") from None
        except Exception as e:
            logger.error(f"Unexpected error in API request: {e}, payload: {payload}, page: {page_number}")
            return None
    
    def _fetch_all_pages(self, payload: Dict[str, Any], records_per_page: int = 20) -> List[Dict]:
        """
        Fetch all pages of results from API
        
        Args:
            payload: Request payload
            records_per_page: Number of records per page (default: 20)
        
        Returns:
            Combined list of all sales orders from all pages
        """
        all_orders = []
        
        # Fetch first page to get total count
        first_page = self._make_request(payload, page_number=1)
        if first_page is None:
            return []
        
        orders = first_page.get('value', [])
        total_count = first_page.get('count', len(orders))
        all_orders.extend(orders)
        
        # Calculate number of pages needed
        if total_count > records_per_page:
            total_pages = (total_count + records_per_page - 1) // records_per_page  # Ceiling division
            logger.info(f"Total records: {total_count}, fetching {total_pages} pages (20 records per page)")
            
            # Fetch remaining pages
            for page_num in range(2, total_pages + 1):
                logger.info(f"  Fetching page {page_num}/{total_pages}...")
                page_result = self._make_request(payload, page_number=page_num)
                if page_result is None:
                    logger.warning(f"  Failed to fetch page {page_num}, continuing...")
                    continue
                
                page_orders = page_result.get('value', [])
                all_orders.extend(page_orders)
                logger.info(f"  ✓ Fetched page {page_num}/{total_pages}: {len(page_orders)} orders")
        
        return all_orders
    
    def fetch_open_salesorders(self) -> List[Dict]:
        """
        Fetch all currently open sales orders (with pagination)
        
        Returns:
            List of open sales orders from all pages
        """
        payload = {"DocumentStatus": "bost_Open"}
        logger.info("Fetching open sales orders from API (with pagination)...")
        all_orders = self._fetch_all_pages(payload)
        logger.info(f"Fetched {len(all_orders)} open sales orders (all pages)")
        return all_orders
    
    def fetch_salesorders_by_date(self, single_date: str) -> List[Dict]:
        """
        Fetch sales orders for a specific date (with pagination)
        
        Args:
            single_date: Date in YYYY-MM-DD format
        
        Returns:
            List of sales orders for that date from all pages
        """
        payload = {"DocDate": single_date}
        logger.info(f"Fetching sales orders for date: {single_date} (with pagination)...")
        all_orders = self._fetch_all_pages(payload)
        logger.info(f"Fetched {len(all_orders)} sales orders for {single_date} (all pages)")
        return all_orders
    
    def fetch_salesorders_by_docnum(self, docnum: int) -> List[Dict]:
        """
        Fetch sales order by document number (with pagination)
        
        Args:
            docnum: Document number
        
        Returns:
            List containing the sales order (or empty if not found) from all pages
        """
        payload = {"DocNum": docnum}
        logger.info(f"Fetching sales order by DocNum: {docnum} (with pagination)...")
        all_orders = self._fetch_all_pages(payload)
        logger.info(f"Fetched {len(all_orders)} sales orders for DocNum {docnum} (all pages)")
        return all_orders
    
    def fetch_last_n_days(self, days: int = 3) -> List[Dict]:
        """
        Fetch sales orders for the last N days (one call per day)
        
        Args:
            days: Number of days to go back (default: 5)
        
        Returns:
            Combined list of all sales orders from the last N days (deduplicated by DocNum)
        """
        all_orders = []
        seen_docnums = set()
        
        for i in range(days):
            date = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
            logger.info(f"Fetching day {i+1}/{days}: {date}")
            orders = self.fetch_salesorders_by_date(date)
            
            # Deduplicate by DocNum
            for order in orders:
                docnum = order.get('DocNum')
                if docnum and docnum not in seen_docnums:
                    all_orders.append(order)
                    seen_docnums.add(docnum)
        
        logger.info(f"Total unique orders from last {days} days: {len(all_orders)}")
        return all_orders
    
    def sync_all_salesorders(self, days_back: int = 3) -> List[Dict]:
        """
        Main sync method: Fetch open orders + new orders from last N days
        
        Args:
            days_back: Number of days to fetch for new orders (default: 3, i.e., today + last 3 days = 4 days total)
        
        Returns:
            Combined and deduplicated list of all sales orders
        """
        all_orders = []
        seen_docnums = set()
        
        # Step 1: Fetch all open orders
        logger.info("Step 1: Fetching open sales orders...")
        open_orders = self.fetch_open_salesorders()
        for order in open_orders:
            docnum = order.get('DocNum')
            if docnum:
                all_orders.append(order)
                seen_docnums.add(docnum)
        
        # Step 2: Fetch new orders from last N days
        logger.info(f"Step 2: Fetching new orders from last {days_back} days...")
        new_orders = self.fetch_last_n_days(days_back)
        for order in new_orders:
            docnum = order.get('DocNum')
            if docnum and docnum not in seen_docnums:
                all_orders.append(order)
                seen_docnums.add(docnum)
        
        logger.info(f"Total unique orders after sync: {len(all_orders)}")
        
        # Step 3: Preload all manufacturers for all orders (batch optimization)
        # Stock is fetched LIVE from Items model in view (not during sync)
        logger.info("Step 3: Preloading manufacturers for all items...")
        all_item_codes = set()
        for order in all_orders:
            for line in order.get('DocumentLines', []):
                item_code = line.get('ItemCode')
                if item_code:
                    all_item_codes.add(str(item_code))
        
        if all_item_codes:
            self._load_manufacturer_cache(list(all_item_codes))
            logger.info(f"Preloaded manufacturers for {len(all_item_codes)} unique items")
        
        return all_orders
    
    def _filter_ho_customers(self, orders: List[Dict]) -> List[Dict]:
        """
        Filter orders to only include those where CardCode starts with "HO" or "SD"
        
        Args:
            orders: List of sales order dictionaries
        
        Returns:
            Filtered list of orders
        """
        filtered = []
        for order in orders:
            card_code = order.get('CardCode', '') or order.get('BusinessPartner', {}).get('CardCode', '')
            if isinstance(card_code, str):
                card_code_upper = card_code.strip().upper()
                if card_code_upper.startswith('HO') or card_code_upper.startswith('SD'):
                    filtered.append(order)
        return filtered
    
    def _load_manufacturer_cache(self, item_codes: List[str]):
        """
        Batch load manufacturers for multiple item codes to avoid N+1 queries
        
        Args:
            item_codes: List of item codes to load
        """
        if not item_codes:
            return
        
        # Filter out already cached items
        uncached_codes = [code for code in item_codes if code and code not in self._manufacturer_cache]
        if not uncached_codes:
            return
        
        try:
            # Batch load all items in one query
            items = Items.objects.filter(item_code__in=uncached_codes).only('item_code', 'item_firm')
            for item in items:
                self._manufacturer_cache[item.item_code] = item.item_firm or ""
            
            # Cache misses (items not found) as empty strings to avoid repeated lookups
            found_codes = set(item.item_code for item in items)
            for code in uncached_codes:
                if code not in found_codes:
                    self._manufacturer_cache[code] = ""
                    
        except Exception as e:
            logger.warning(f"Error batch loading manufacturers: {e}")
    
    def _load_stock_cache(self, item_codes: List[str]):
        """
        Batch load stock (total_available_stock, dip_warehouse_stock) from Items model
        to avoid N+1 queries.
        
        Args:
            item_codes: List of item codes to load
        """
        if not item_codes:
            return
        
        # Filter out already cached items
        uncached_codes = [code for code in item_codes if code and code not in self._stock_cache]
        if not uncached_codes:
            return
        
        try:
            items = Items.objects.filter(item_code__in=uncached_codes).only(
                'item_code', 'total_available_stock', 'dip_warehouse_stock'
            )
            for item in items:
                # Convert Decimal to float for JSON serialization
                self._stock_cache[item.item_code] = {
                    'total_available_stock': float(item.total_available_stock or 0),
                    'dip_warehouse_stock': float(item.dip_warehouse_stock or 0),
                }
            # Cache misses (items not found)
            for code in uncached_codes:
                if code not in self._stock_cache:
                    self._stock_cache[code] = {
                        'total_available_stock': 0.0,
                        'dip_warehouse_stock': 0.0,
                    }
        except Exception as e:
            logger.warning(f"Error batch loading stock: {e}")
    
    def _get_stock_from_item_code(self, item_code: str) -> Dict[str, float]:
        """
        Lookup stock from Items model by item_code (uses cache)
        
        Args:
            item_code: Item code from API
        
        Returns:
            Dict with 'total_available_stock' and 'dip_warehouse_stock' (defaults to 0 if not found)
        """
        if not item_code:
            return {'total_available_stock': 0.0, 'dip_warehouse_stock': 0.0}
        
        # Check cache first
        if item_code in self._stock_cache:
            return self._stock_cache[item_code]
        
        # Fallback: single query (should rarely happen if batch loading is used)
        try:
            item = Items.objects.only('total_available_stock', 'dip_warehouse_stock').get(item_code=item_code)
            stock_data = {
                'total_available_stock': float(item.total_available_stock or 0),
                'dip_warehouse_stock': float(item.dip_warehouse_stock or 0),
            }
            self._stock_cache[item_code] = stock_data
            return stock_data
        except Items.DoesNotExist:
            logger.debug(f"Item not found in Items table for stock lookup: {item_code}")
            stock_data = {'total_available_stock': 0.0, 'dip_warehouse_stock': 0.0}
            self._stock_cache[item_code] = stock_data
            return stock_data
        except Exception as e:
            logger.warning(f"Error looking up stock for item {item_code}: {e}")
            return {'total_available_stock': 0.0, 'dip_warehouse_stock': 0.0}
    
    def _get_manufacturer_from_item_code(self, item_code: str) -> str:
        """
        Lookup manufacturer from Items model by item_code (uses cache)
        
        Args:
            item_code: Item code from API
        
        Returns:
            Manufacturer (item_firm) or empty string if not found
        """
        if not item_code:
            return ""
        
        # Check cache first
        if item_code in self._manufacturer_cache:
            return self._manufacturer_cache[item_code]
        
        # Fallback: single query (should rarely happen if batch loading is used)
        try:
            item = Items.objects.only('item_firm').get(item_code=item_code)
            manufacturer = item.item_firm or ""
            self._manufacturer_cache[item_code] = manufacturer
            return manufacturer
        except Items.DoesNotExist:
            logger.debug(f"Item not found in Items table: {item_code}")
            self._manufacturer_cache[item_code] = ""  # Cache miss
            return ""
        except Exception as e:
            logger.warning(f"Error looking up item {item_code}: {e}")
            return ""
    
    def _map_api_response_to_model(self, api_order: Dict) -> Dict:
        """
        Map API response to Django model format
        
        Args:
            api_order: Single sales order from API response
        
        Returns:
            Dictionary with mapped fields for SAPSalesorder and SAPSalesorderItem
        """
        # Extract header fields
        docnum = str(api_order.get('DocNum', ''))
        doc_entry = str(api_order.get('DocEntry', '')) if api_order.get('DocEntry') else None
        
        # Date parsing
        doc_date_str = api_order.get('DocDate', '')
        posting_date = None
        if doc_date_str:
            try:
                # Try parsing YYYY-MM-DD format
                posting_date = datetime.strptime(doc_date_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                try:
                    # Try other common formats
                    posting_date = datetime.strptime(doc_date_str, '%Y/%m/%d').date()
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse date: {doc_date_str}")
        
        # Business Partner
        bp = api_order.get('BusinessPartner', {})
        customer_code = bp.get('CardCode', '') or api_order.get('CardCode', '')
        customer_name = bp.get('CardName', '') or api_order.get('CardName', '')
        # Extract VAT Number from BusinessPartner.FederalTaxID
        vat_number = str(bp.get('FederalTaxID', '')).strip() if bp.get('FederalTaxID') else ''
        # Extract Phone1 from BusinessPartner
        customer_phone = str(bp.get('Phone1', '')).strip() if bp.get('Phone1') else ''
        # Extract Address from main API response (where DocNum, DocDate are)
        customer_address = str(api_order.get('Address', '')).strip() if api_order.get('Address') else ''
        # Extract ClosingRemarks from API response
        closing_remarks = str(api_order.get('ClosingRemarks', '')).strip() if api_order.get('ClosingRemarks') else ''
        # Replace \r with \n for proper line breaks
        if closing_remarks:
            closing_remarks = closing_remarks.replace('\r', '\n')
        
        # Sales Person
        sales_person = api_order.get('SalesPerson', {})
        salesman_name = sales_person.get('SalesEmployeeName', '') or api_order.get('SalesPersonCode', '')
        
        # Other header fields
        # BP Reference: Use NumAtCard from API (fallback to U_PurchaseOrder if not available)
        bp_reference = api_order.get('NumAtCard', '') or api_order.get('U_PurchaseOrder', '') or ''
        # Check if this SO has a Proforma Invoice created in SAP
        is_sap_pi = api_order.get('U_PROFORMAINVOICE', '') == 'Y' or api_order.get('U_PROFORMAINVOICE', '') == True
        # SAP PI LPO Date (only meaningful when is_sap_pi=True)
        lp_date_str = api_order.get('U_Lpdate', '') or ''
        sap_pi_lpo_date = None
        if lp_date_str:
            try:
                sap_pi_lpo_date = datetime.strptime(lp_date_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                try:
                    sap_pi_lpo_date = datetime.strptime(lp_date_str, '%Y/%m/%d').date()
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse U_Lpdate: {lp_date_str}")
        # Extract NFRef from TaxExtension
        tax_extension = api_order.get('TaxExtension') or {}
        nf_ref = ''
        if isinstance(tax_extension, dict):
            nf_ref_raw = tax_extension.get('NFRef')
            if nf_ref_raw:
                nf_ref = str(nf_ref_raw).strip()
        # Log if NFRef is missing (for debugging)
        if not nf_ref and logger:
            logger.debug(f"SO {api_order.get('DocNum')}: No NFRef found in TaxExtension")
        doc_total = api_order.get('DocTotal', 0) or 0
        vat_sum = api_order.get('VatSum', 0) or 0
        total_discount = api_order.get('TotalDiscount', 0) or 0
        
        # Discount Percent - round to 1 decimal for display, but use exact value for calculations
        discount_percent_raw = api_order.get('DiscountPercent', 0) or 0
        try:
            discount_percent_exact = float(discount_percent_raw)
            # Round to 1 decimal place for display (e.g., 1.998446 → 2.0)
            discount_percent_display = round(discount_percent_exact, 1)
        except (ValueError, TypeError):
            discount_percent_exact = 0.0
            discount_percent_display = 0.0
        
        # Document Status mapping
        doc_status = api_order.get('DocumentStatus', '')
        status = "O" if doc_status == "bost_Open" else "C"
        
        # Map document lines
        document_lines = api_order.get('DocumentLines', [])
        items = []
        
        # Batch load manufacturers for all items in this order (optimization)
        item_codes = [str(line.get('ItemCode', '')) for line in document_lines if line.get('ItemCode')]
        if item_codes:
            self._load_manufacturer_cache(item_codes)
        
        for idx, line in enumerate(document_lines):
            item_code = str(line.get('ItemCode', '')) if line.get('ItemCode') else ''
            
            # Lookup manufacturer from cache (already loaded above)
            manufacture = self._get_manufacturer_from_item_code(item_code)
            
            # Line Status mapping
            line_status = line.get('LineStatus', '')
            row_status = "O" if line_status == "bost_Open" else "C"
            
            # Line number (0-based in API, convert to 1-based)
            line_num = line.get('LineNum', idx)
            if isinstance(line_num, int):
                line_no = line_num + 1 if line_num >= 0 else idx + 1
            else:
                line_no = idx + 1
            
            # Get RemainingOpenQuantity from API (for remaining_open_quantity)
            remaining_open_qty = line.get('RemainingOpenQuantity', 0) or 0
            
            # Get quantity, price, and line total
            quantity = line.get('Quantity', 0) or 0
            price = line.get('Price', 0) or 0
            line_total = line.get('LineTotal', 0) or 0
            
            # Calculate pending_amount from RemainingOpenQuantity
            # Formula: pending_amount = RemainingOpenQuantity * Price
            pending_amount = remaining_open_qty * price
            
            item_data = {
                'line_no': line_no,
                'item_no': item_code,
                'description': line.get('ItemDescription', '') or '',
                'quantity': quantity,
                'price': line.get('Price', 0) or 0,
                'row_total': line_total,
                'row_status': row_status,
                'manufacture': manufacture,
                'job_type': '',  # Not in API response
                'remaining_open_quantity': remaining_open_qty,  # Use RemainingOpenQuantity from API
                'pending_amount': pending_amount,  # Calculate from RemainingOpenQuantity
                # Stock is fetched LIVE from Items model in view (not stored in SAPSalesorderItem)
            }
            
            items.append(item_data)
        
        # Calculate row_total_sum from items (this is the Subtotal)
        row_total_sum = sum(item.get('row_total', 0) for item in items)
        
        # Calculate pending_total = sum of OpenAmount from all items
        pending_total = sum(item.get('pending_amount', 0) for item in items)
        
        # If pending_total is 0, use doc_total (for closed orders or if OpenAmount not available)
        if pending_total == 0:
            pending_total = doc_total
        
        return {
            'so_number': docnum,
            'internal_number': doc_entry,
            'posting_date': posting_date,
            'customer_code': customer_code or '',
            'customer_name': customer_name or '',
            'salesman_name': salesman_name or '',
            'bp_reference_no': bp_reference or '',
            'vat_number': vat_number,  # VAT Number from BusinessPartner.FederalTaxID
            'customer_address': customer_address,  # Address from main API response
            'customer_phone': customer_phone,  # Phone1 from BusinessPartner
            'closing_remarks': closing_remarks,  # ClosingRemarks from API
            'is_sap_pi': is_sap_pi,  # True if U_PROFORMAINVOICE=Y
            'sap_pi_lpo_date': sap_pi_lpo_date,  # From API field U_Lpdate (date or None)
            'nf_ref': nf_ref,  # NFRef from TaxExtension - quotation reference
            'document_total': pending_total,  # Pending total = sum of OpenAmount
            'row_total_sum': row_total_sum,  # Subtotal (calculated from items)
            'discount_percentage': discount_percent_exact,  # Exact value for calculations
            'discount_percentage_display': discount_percent_display,  # Rounded to 1 decimal for display
            'vat_sum': vat_sum,  # VatSum from API
            'total_discount': total_discount,  # TotalDiscount from API
            'doc_total_full': doc_total,  # Full DocTotal from API (for reference)
            'status': status,
            'items': items,
        }

    # ----- Purchase Order API (same structure as Sales Order, different base URL) -----
    def _get_po_base_url(self) -> str:
        return getattr(settings, 'SAP_PURCHASE_ORDER_API_URL', 'http://192.168.1.103/IntegrationApi/api/PurchaseOrder')

    def fetch_open_purchaseorders(self) -> List[Dict]:
        """
        Fetch all currently open purchase orders (with pagination).
        """
        payload = {"DocumentStatus": "bost_Open"}
        base_url = self._get_po_base_url()
        logger.info("Fetching open purchase orders from API (with pagination)...")
        all_orders = self._fetch_all_pages_with_url(payload, base_url)
        logger.info(f"Fetched {len(all_orders)} open purchase orders (all pages)")
        return all_orders

    def fetch_purchaseorders_by_date(self, single_date: str) -> List[Dict]:
        """
        Fetch purchase orders for a specific date (with pagination).
        Uses FromDate/ToDate with same date for single day.
        """
        payload = {"FromDate": single_date, "ToDate": single_date}
        base_url = self._get_po_base_url()
        logger.info(f"Fetching purchase orders for date: {single_date} (with pagination)...")
        all_orders = self._fetch_all_pages_with_url(payload, base_url)
        logger.info(f"Fetched {len(all_orders)} purchase orders for {single_date} (all pages)")
        return all_orders

    def fetch_purchaseorders_by_date_range(self, from_date: str, to_date: str) -> List[Dict]:
        """
        Fetch purchase orders for a date range (with pagination).
        """
        payload = {"FromDate": from_date, "ToDate": to_date}
        base_url = self._get_po_base_url()
        logger.info(f"Fetching purchase orders for date range: {from_date} to {to_date} (with pagination)...")
        all_orders = self._fetch_all_pages_with_url(payload, base_url)
        logger.info(f"Fetched {len(all_orders)} purchase orders for {from_date} to {to_date} (all pages)")
        return all_orders

    def fetch_purchaseorders_by_docnum(self, docnum: int) -> List[Dict]:
        """
        Fetch purchase order by document number (with pagination).
        """
        payload = {"DocNum": docnum}
        base_url = self._get_po_base_url()
        logger.info(f"Fetching purchase order by DocNum: {docnum} (with pagination)...")
        all_orders = self._fetch_all_pages_with_url(payload, base_url)
        logger.info(f"Fetched {len(all_orders)} purchase orders for DocNum {docnum} (all pages)")
        return all_orders

    def _map_purchaseorder_api_response(self, api_order: Dict) -> Dict:
        """
        Map Purchase Order API response to Django model format (SAPPurchaseOrder / SAPPurchaseOrderItem).
        """
        docnum = str(api_order.get('DocNum', ''))
        doc_entry = str(api_order.get('DocEntry', '')) if api_order.get('DocEntry') else None

        doc_date_str = api_order.get('DocDate', '')
        posting_date = None
        if doc_date_str:
            try:
                posting_date = datetime.strptime(doc_date_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                try:
                    posting_date = datetime.strptime(doc_date_str, '%Y/%m/%d').date()
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse date: {doc_date_str}")

        bp = api_order.get('BusinessPartner', {})
        supplier_code = bp.get('CardCode', '') or api_order.get('CardCode', '')
        supplier_name = bp.get('CardName', '') or api_order.get('CardName', '')
        vat_number = str(bp.get('FederalTaxID', '')).strip() if bp.get('FederalTaxID') else ''
        supplier_phone = str(bp.get('Phone1', '')).strip() if bp.get('Phone1') else ''
        supplier_address = str(api_order.get('Address', '')).strip() if api_order.get('Address') else ''
        closing_remarks = str(api_order.get('ClosingRemarks', '')).strip() if api_order.get('ClosingRemarks') else ''
        if closing_remarks:
            closing_remarks = closing_remarks.replace('\r', '\n')

        sales_person = api_order.get('SalesPerson', {})
        salesman_name = sales_person.get('SalesEmployeeName', '') or api_order.get('SalesPersonCode', '')

        # LPO reference from NumAtCard only (e.g. "V2092666/5275-6/6320/1311"); if not present use "Not mentioned"
        num_at_card = api_order.get('NumAtCard')
        bp_reference = str(num_at_card).strip() if num_at_card else 'Not mentioned'
        doc_total = api_order.get('DocTotal', 0) or 0
        vat_sum = api_order.get('VatSum', 0) or 0
        total_discount = api_order.get('TotalDiscount', 0) or 0

        discount_percent_raw = api_order.get('DiscountPercent', 0) or 0
        try:
            discount_percent_exact = float(discount_percent_raw)
        except (ValueError, TypeError):
            discount_percent_exact = 0.0

        doc_status = api_order.get('DocumentStatus', '')
        status = "O" if doc_status == "bost_Open" else "C"

        document_lines = api_order.get('DocumentLines', [])
        items = []

        for idx, line in enumerate(document_lines):
            item_code = str(line.get('ItemCode', '')) if line.get('ItemCode') else ''
            line_status = line.get('LineStatus', '')
            row_status = "O" if line_status == "bost_Open" else "C"

            line_num = line.get('LineNum', idx)
            if isinstance(line_num, int):
                line_no = line_num + 1 if line_num >= 0 else idx + 1
            else:
                line_no = idx + 1

            remaining_open_qty = line.get('RemainingOpenQuantity', 0) or 0
            quantity = line.get('Quantity', 0) or 0
            price = line.get('Price', 0) or 0
            line_total = line.get('LineTotal', 0) or 0
            pending_amount = remaining_open_qty * price

            item_data = {
                'line_no': line_no,
                'item_no': item_code,
                'description': line.get('ItemDescription', '') or '',
                'quantity': quantity,
                'price': price,
                'row_total': line_total,
                'row_status': row_status,
                'manufacture': '',
                'job_type': '',
                'remaining_open_quantity': remaining_open_qty,
                'pending_amount': pending_amount,
            }
            items.append(item_data)

        row_total_sum = sum(item.get('row_total', 0) for item in items)
        pending_total = sum(item.get('pending_amount', 0) for item in items)
        if pending_total == 0:
            pending_total = doc_total

        return {
            'po_number': docnum,
            'internal_number': doc_entry,
            'posting_date': posting_date,
            'supplier_code': supplier_code or '',
            'supplier_name': supplier_name or '',
            'supplier_address': supplier_address,
            'supplier_phone': supplier_phone,
            'vat_number': vat_number,
            'bp_reference_no': bp_reference or '',
            'salesman_name': salesman_name or '',
            'discount_percentage': discount_percent_exact,
            'document_total': pending_total,
            'row_total_sum': row_total_sum,
            'vat_sum': vat_sum,
            'total_discount': total_discount,
            'status': status,
            'closing_remarks': closing_remarks,
            'items': items,
        }

    def fetch_arinvoices_by_date_range(self, from_date: str, to_date: str) -> List[Dict]:
        """
        Fetch AR Invoices for a date range (with pagination)
        
        Args:
            from_date: Start date in YYYY-MM-DD format
            to_date: End date in YYYY-MM-DD format
        
        Returns:
            List of AR invoices for that date range from all pages
        """
        payload = {"FromDate": from_date, "ToDate": to_date}
        base_url = getattr(settings, 'SAP_AR_INVOICE_API_URL', 'http://192.168.1.103/IntegrationApi/api/ARInvoice')
        logger.info(f"Fetching AR invoices for date range: {from_date} to {to_date} (with pagination)...")
        all_invoices = self._fetch_all_pages_with_url(payload, base_url)
        logger.info(f"Fetched {len(all_invoices)} AR invoices for {from_date} to {to_date} (all pages)")
        return all_invoices
    
    def fetch_arinvoices_last_n_days(self, days: int = 3) -> List[Dict]:
        """
        Fetch AR Invoices for the last N days (one call per day range)
        
        Args:
            days: Number of days to go back (default: 3)
        
        Returns:
            Combined list of all AR invoices from the last N days (deduplicated by DocNum)
        """
        all_invoices = []
        seen_docnums = set()
        
        # Calculate date range: from (today - days) to today
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days-1)  # Include today + last N-1 days
        
        from_date_str = start_date.strftime('%Y-%m-%d')
        to_date_str = end_date.strftime('%Y-%m-%d')
        
        logger.info(f"Fetching AR invoices for last {days} days: {from_date_str} to {to_date_str}")
        invoices = self.fetch_arinvoices_by_date_range(from_date_str, to_date_str)
        
        # Deduplicate by DocNum
        for invoice in invoices:
            docnum = invoice.get('DocNum')
            if docnum and docnum not in seen_docnums:
                all_invoices.append(invoice)
                seen_docnums.add(docnum)
        
        logger.info(f"Total unique AR invoices from last {days} days: {len(all_invoices)}")
        return all_invoices
    
    def fetch_arcreditmemos_by_date_range(self, from_date: str, to_date: str) -> List[Dict]:
        """
        Fetch AR Credit Memos for a date range (with pagination)
        
        Args:
            from_date: Start date in YYYY-MM-DD format
            to_date: End date in YYYY-MM-DD format
        
        Returns:
            List of AR credit memos for that date range from all pages
        """
        payload = {"FromDate": from_date, "ToDate": to_date}
        base_url = getattr(settings, 'SAP_AR_CREDIT_MEMO_API_URL', 'http://192.168.1.103/IntegrationApi/api/ARCreditMemo')
        logger.info(f"Fetching AR credit memos for date range: {from_date} to {to_date} (with pagination)...")
        all_creditmemos = self._fetch_all_pages_with_url(payload, base_url)
        logger.info(f"Fetched {len(all_creditmemos)} AR credit memos for {from_date} to {to_date} (all pages)")
        return all_creditmemos
    
    def fetch_arcreditmemos_last_n_days(self, days: int = 3) -> List[Dict]:
        """
        Fetch AR Credit Memos for the last N days (one call per day range)
        
        Args:
            days: Number of days to go back (default: 3)
        
        Returns:
            Combined list of all AR credit memos from the last N days (deduplicated by DocNum)
        """
        all_creditmemos = []
        seen_docnums = set()
        
        # Calculate date range: from (today - days) to today
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days-1)  # Include today + last N-1 days
        
        from_date_str = start_date.strftime('%Y-%m-%d')
        to_date_str = end_date.strftime('%Y-%m-%d')
        
        logger.info(f"Fetching AR credit memos for last {days} days: {from_date_str} to {to_date_str}")
        creditmemos = self.fetch_arcreditmemos_by_date_range(from_date_str, to_date_str)
        
        # Deduplicate by DocNum
        for creditmemo in creditmemos:
            docnum = creditmemo.get('DocNum')
            if docnum and docnum not in seen_docnums:
                all_creditmemos.append(creditmemo)
                seen_docnums.add(docnum)
        
        logger.info(f"Total unique AR credit memos from last {days} days: {len(all_creditmemos)}")
        return all_creditmemos
    
    def _fetch_all_pages_with_url(self, payload: Dict[str, Any], base_url: str, records_per_page: int = 20) -> List[Dict]:
        """
        Fetch all pages of results from API using a specific base URL
        
        Args:
            payload: Request payload
            base_url: Base URL for the API endpoint
            records_per_page: Number of records per page (default: 20)
        
        Returns:
            Combined list of all records from all pages
        """
        all_records = []
        
        # Fetch first page to get total count
        first_page = self._make_request_with_url(payload, base_url, page_number=1)
        if first_page is None:
            return []
        
        records = first_page.get('value', [])
        total_count = first_page.get('count', len(records))
        all_records.extend(records)
        
        # Calculate number of pages needed
        if total_count > records_per_page:
            total_pages = (total_count + records_per_page - 1) // records_per_page  # Ceiling division
            logger.info(f"Total records: {total_count}, fetching {total_pages} pages (20 records per page)")
            
            # Fetch remaining pages
            for page_num in range(2, total_pages + 1):
                logger.info(f"  Fetching page {page_num}/{total_pages}...")
                page_result = self._make_request_with_url(payload, base_url, page_number=page_num)
                if page_result is None:
                    logger.warning(f"  Failed to fetch page {page_num}, continuing...")
                    continue
                
                page_records = page_result.get('value', [])
                all_records.extend(page_records)
                logger.info(f"  ✓ Fetched page {page_num}/{total_pages}: {len(page_records)} records")
        
        return all_records
    
    def _make_request_with_url(self, payload: Dict[str, Any], base_url: str, page_number: int = 1) -> Optional[Dict]:
        """
        Make POST request to SAP API with pagination support using a specific base URL
        
        Args:
            payload: Request payload
            base_url: Base URL for the API endpoint
            page_number: Page number to fetch (default: 1)
        
        Returns:
            Dictionary with 'value' (list of records), 'count' (total count), or None if error
        """
        try:
            # Add pageNumber to payload
            request_payload = payload.copy()
            if page_number > 1:
                request_payload['pageNumber'] = page_number
            
            response = requests.post(
                base_url,
                json=request_payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            
            # API returns data in 'value' key with 'odata.count' for total
            if isinstance(data, dict):
                return {
                    'value': data.get('value', []),
                    'count': int(data.get('odata.count', 0)) if data.get('odata.count') else 0
                }
            elif isinstance(data, list):
                return {
                    'value': data,
                    'count': len(data)
                }
            else:
                logger.warning(f"Unexpected API response format: {data}")
                return {'value': [], 'count': 0}
                
        except requests.exceptions.Timeout:
            logger.error(f"API request timeout after {self.timeout}s: {payload}, page: {page_number}, url: {base_url}")
            raise RuntimeError(f"SAP API timeout ({self.timeout}s). Check if SSH tunnel is connected.") from None
        except requests.exceptions.ConnectionError as e:
            logger.error(f"API connection error: {e}, payload: {payload}, page: {page_number}, url: {base_url}")
            raise RuntimeError("Cannot connect to SAP API. Is the SSH tunnel running? (ssh -N -R 8443:192.168.1.103:80 root@VPS)") from None
        except requests.exceptions.RequestException as e:
            logger.error(f"API request error: {e}, payload: {payload}, page: {page_number}, url: {base_url}")
            raise RuntimeError(f"SAP API error: {e}") from None
        except Exception as e:
            logger.error(f"Unexpected error in API request: {e}, payload: {payload}, page: {page_number}, url: {base_url}")
            return None
    def _ensure_item_exists(self, item_code: str, item_description: str, upc_code: str = None):
        """
        Ensure an item exists in Items table. If not, create it and remove from IgnoreList.
        
        Args:
            item_code: Item code from API
            item_description: Item description from API
            upc_code: UPC code from API (U_UPCCODE)
        
        Returns:
            Items instance (existing or newly created)
        """
        if not item_code:
            return None
        
        try:
            # Try to get existing item
            item = Items.objects.get(item_code=item_code)
            return item
        except Items.DoesNotExist:
            # Create new item
            try:
                item = Items.objects.create(
                    item_code=item_code,
                    item_description=item_description[:100] if len(item_description) > 100 else item_description,
                    item_upvc=upc_code or '',
                    item_cost=0.0,
                    item_firm='',
                    item_price=0.0,
                    item_stock=0,
                    total_available_stock=None,
                    dip_warehouse_stock=None
                )
                logger.info(f"Auto-created Items record for item_code: {item_code}")
                
                # Remove from IgnoreList if exists
                try:
                    IgnoreList.objects.filter(item_code=item_code).delete()
                    logger.info(f"Removed item_code {item_code} from IgnoreList")
                except Exception as e:
                    logger.warning(f"Error removing {item_code} from IgnoreList: {e}")
                
                return item
            except Exception as e:
                logger.error(f"Error creating Items record for {item_code}: {e}")
                return None
    
    def _clamp_percentage(self, value: Any, field_name: str = 'percentage') -> float:
        """
        Clamp percentage value to valid range for DecimalField(max_digits=5, decimal_places=2)
        Valid range: -999.99 to 999.99
        
        Args:
            value: The percentage value to clamp
            field_name: Name of the field (for logging)
        
        Returns:
            Clamped value within -999.99 to 999.99
        """
        try:
            if value is None:
                return 0.0
            num_value = float(value)
            # Clamp to valid range for max_digits=5, decimal_places=2
            # Max absolute value is 999.99 (10^3 - 0.01)
            clamped = max(-999.99, min(999.99, num_value))
            if abs(num_value - clamped) > 0.01:  # Only log if there was a significant change
                logger.warning(
                    f"Clamped {field_name} value from {num_value} to {clamped} "
                    f"(exceeds DecimalField(max_digits=5, decimal_places=2) range)"
                )
            return clamped
        except (ValueError, TypeError):
            logger.warning(f"Invalid {field_name} value: {value}, using 0.0")
            return 0.0
    
    def _map_arinvoice_api_response(self, api_invoice: Dict) -> Dict:
        """
        Map API response to Django model format for AR Invoice
        
        Args:
            api_invoice: Single AR invoice from API response
        
        Returns:
            Dictionary with mapped fields for SAPARInvoice and SAPARInvoiceItem
        """
        # Extract header fields
        docnum = str(api_invoice.get('DocNum', ''))
        doc_entry = str(api_invoice.get('DocEntry', '')) if api_invoice.get('DocEntry') else None
        
        # Date parsing
        doc_date_str = api_invoice.get('DocDate', '')
        posting_date = None
        if doc_date_str:
            try:
                posting_date = datetime.strptime(doc_date_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                try:
                    posting_date = datetime.strptime(doc_date_str, '%Y/%m/%d').date()
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse DocDate: {doc_date_str}")
        
        doc_due_date_str = api_invoice.get('DocDueDate', '')
        doc_due_date = None
        if doc_due_date_str:
            try:
                doc_due_date = datetime.strptime(doc_due_date_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                try:
                    doc_due_date = datetime.strptime(doc_due_date_str, '%Y/%m/%d').date()
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse DocDueDate: {doc_due_date_str}")
        
        # Business Partner
        bp = api_invoice.get('BusinessPartner', {})
        customer_code = bp.get('CardCode', '') or api_invoice.get('CardCode', '')
        customer_name = bp.get('CardName', '') or api_invoice.get('CardName', '')
        vat_number = str(bp.get('FederalTaxID', '')).strip() if bp.get('FederalTaxID') else ''
        
        # Sales Person
        sales_person = api_invoice.get('SalesPerson', {})
        salesman_name = sales_person.get('SalesEmployeeName', '') or ''
        salesman_code = sales_person.get('SalesEmployeeCode') or None
        
        # Calculate store based on salesman_name
        # If salesman_name starts with 'R.' or 'E.', store = 'Others', else 'HO'
        store = 'Others' if salesman_name and (salesman_name.strip().startswith('R.') or salesman_name.strip().startswith('E.')) else 'HO'
        
        # Other header fields
        customer_address = str(api_invoice.get('Address', '')).strip() if api_invoice.get('Address') else ''
        bp_reference = api_invoice.get('NumAtCard', '') or ''
        doc_total = api_invoice.get('DocTotal', 0) or 0
        vat_sum = api_invoice.get('VatSum', 0) or 0
        total_discount = api_invoice.get('TotalDiscount', 0) or 0
        discount_percent = self._clamp_percentage(api_invoice.get('DiscountPercent', 0) or 0, 'discount_percent')
        cancel_status = api_invoice.get('CancelStatus', '') or ''
        document_status = api_invoice.get('DocumentStatus', '') or ''
        comments = str(api_invoice.get('Comments', '')).strip() if api_invoice.get('Comments') else ''
        rounding_diff_amount = api_invoice.get('RoundingDiffAmount', 0) or 0
        
        # Calculate doc_total_without_vat (Subtotal AFTER discount)
        # RoundingDiffAmount (if negative like -10.4) should be SUBTRACTED from doc_total_without_vat
        # Formula: doc_total_without_vat = DocTotal - VATSum - RoundingDiffAmount
        # Example: DocTotal - VATSum - (-10.4) = DocTotal - VATSum + 10.4 (gets actual subtotal)
        doc_total_without_vat = doc_total - vat_sum
        if rounding_diff_amount:
            doc_total_without_vat = doc_total_without_vat - rounding_diff_amount
        
        # AR Invoice: If cancel_status is 'csCancellation', make all amounts negative
        # This reverses the original invoice, so totals will be correct
        sign_multiplier = -1 if cancel_status == 'csCancellation' else 1
        
        # Apply sign to header amounts
        doc_total = doc_total * sign_multiplier
        doc_total_without_vat = doc_total_without_vat * sign_multiplier
        vat_sum = vat_sum * sign_multiplier
        total_discount = total_discount * sign_multiplier
        # Also apply sign to rounding_diff_amount for consistency
        rounding_diff_amount = rounding_diff_amount * sign_multiplier
        
        # Calculate subtotal_before_discount (Subtotal BEFORE discount)
        # This is the subtotal before discount is applied (after sign multiplier)
        subtotal_before_discount = doc_total_without_vat + total_discount
        
        # Map document lines
        document_lines = api_invoice.get('DocumentLines', [])
        items = []
        
        for idx, line in enumerate(document_lines):
            item_code = str(line.get('ItemCode', '')) if line.get('ItemCode') else ''
            item_description = line.get('ItemDescription', '') or ''
            upc_code = line.get('U_UPCCODE', '') or ''
            
            # Ensure item exists in Items table
            item_obj = self._ensure_item_exists(item_code, item_description, upc_code)
            
            # Line number (0-based in API, convert to 1-based)
            line_num = line.get('LineNum', idx)
            if isinstance(line_num, int):
                line_no = line_num + 1 if line_num >= 0 else idx + 1
            else:
                line_no = idx + 1
            
            quantity = line.get('Quantity', 0) or 0
            price = line.get('Price', 0) or 0
            price_after_vat = line.get('PriceAfterVAT', 0) or 0
            discount_percent_line = self._clamp_percentage(line.get('DiscountPercent', 0) or 0, 'discount_percent_line')
            line_total = line.get('LineTotal', 0) or 0
            cost_price = line.get('GrossProfitTotalBasePrice', 0) or 0  # Total cost price for this line
            tax_percentage = self._clamp_percentage(line.get('TaxPercentagePerRow', 0) or 0, 'tax_percentage')
            tax_total = line.get('TaxTotal', 0) or 0
            
            # Calculate line_total_after_discount if header discount exists
            # Discount is applied at header level, but we show line_total_after_discount for viewing
            line_total_after_discount = line_total
            if discount_percent and discount_percent > 0:
                line_total_after_discount = line_total * (1 - discount_percent / 100)
            
            # Calculate Gross Profit = LineTotal after discount - cost_price
            gross_profit = line_total_after_discount - cost_price
            
            # Apply sign multiplier to item amounts (for csCancellation)
            price = price * sign_multiplier
            price_after_vat = price_after_vat * sign_multiplier
            line_total = line_total * sign_multiplier
            line_total_after_discount = line_total_after_discount * sign_multiplier
            cost_price = cost_price * sign_multiplier
            gross_profit = gross_profit * sign_multiplier
            tax_total = tax_total * sign_multiplier
            
            item_data = {
                'line_no': line_no,
                'item_code': item_code,
                'item_description': item_description,
                'quantity': quantity,
                'price': price,
                'price_after_vat': price_after_vat,
                'discount_percent': discount_percent_line,
                'line_total': line_total,
                'line_total_after_discount': line_total_after_discount,
                'cost_price': cost_price,
                'gross_profit': gross_profit,
                'tax_percentage': tax_percentage,
                'tax_total': tax_total,
                'upc_code': upc_code,
                'item_id': item_obj.id if item_obj else None,  # For ForeignKey
            }
            
            items.append(item_data)
        
        # Calculate total gross profit (sum of all item gross_profit)
        total_gross_profit = sum(item.get('gross_profit', 0) or 0 for item in items)
        
        return {
            'invoice_number': docnum,
            'internal_number': doc_entry,
            'posting_date': posting_date,
            'doc_due_date': doc_due_date,
            'customer_code': customer_code or '',
            'customer_name': customer_name or '',
            'customer_address': customer_address,
            'salesman_name': salesman_name or '',
            'salesman_code': salesman_code,
            'store': store,
            'bp_reference_no': bp_reference or '',
            'doc_total': doc_total,
            'doc_total_without_vat': doc_total_without_vat,
            'subtotal_before_discount': subtotal_before_discount,
            'vat_sum': vat_sum,
            'total_gross_profit': total_gross_profit,
            'discount_percent': discount_percent,
            'cancel_status': cancel_status,
            'document_status': document_status,
            'vat_number': vat_number,
            'comments': comments,
            'rounding_diff_amount': rounding_diff_amount,
            'items': items,
        }
    
    def _map_arcreditmemo_api_response(self, api_creditmemo: Dict) -> Dict:
        """
        Map API response to Django model format for AR Credit Memo
        
        Args:
            api_creditmemo: Single AR credit memo from API response
        
        Returns:
            Dictionary with mapped fields for SAPARCreditMemo and SAPARCreditMemoItem
        """
        # Extract header fields
        docnum = str(api_creditmemo.get('DocNum', ''))
        doc_entry = str(api_creditmemo.get('DocEntry', '')) if api_creditmemo.get('DocEntry') else None
        
        # Date parsing
        doc_date_str = api_creditmemo.get('DocDate', '')
        posting_date = None
        if doc_date_str:
            try:
                posting_date = datetime.strptime(doc_date_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                try:
                    posting_date = datetime.strptime(doc_date_str, '%Y/%m/%d').date()
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse DocDate: {doc_date_str}")
        
        doc_due_date_str = api_creditmemo.get('DocDueDate', '')
        doc_due_date = None
        if doc_due_date_str:
            try:
                doc_due_date = datetime.strptime(doc_due_date_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                try:
                    doc_due_date = datetime.strptime(doc_due_date_str, '%Y/%m/%d').date()
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse DocDueDate: {doc_due_date_str}")
        
        # Business Partner
        bp = api_creditmemo.get('BusinessPartner', {})
        customer_code = bp.get('CardCode', '') or api_creditmemo.get('CardCode', '')
        customer_name = bp.get('CardName', '') or api_creditmemo.get('CardName', '')
        vat_number = str(bp.get('FederalTaxID', '')).strip() if bp.get('FederalTaxID') else ''
        
        # Sales Person
        sales_person = api_creditmemo.get('SalesPerson', {})
        salesman_name = sales_person.get('SalesEmployeeName', '') or ''
        salesman_code = sales_person.get('SalesEmployeeCode') or None
        
        # Calculate store based on salesman_name
        # If salesman_name starts with 'R.' or 'E.', store = 'Others', else 'HO'
        store = 'Others' if salesman_name and (salesman_name.strip().startswith('R.') or salesman_name.strip().startswith('E.')) else 'HO'
        
        # Other header fields
        customer_address = str(api_creditmemo.get('Address', '')).strip() if api_creditmemo.get('Address') else ''
        bp_reference = api_creditmemo.get('NumAtCard', '') or ''
        doc_total = api_creditmemo.get('DocTotal', 0) or 0
        vat_sum = api_creditmemo.get('VatSum', 0) or 0
        total_discount = api_creditmemo.get('TotalDiscount', 0) or 0
        discount_percent = self._clamp_percentage(api_creditmemo.get('DiscountPercent', 0) or 0, 'discount_percent')
        cancel_status = api_creditmemo.get('CancelStatus', '') or ''
        document_status = api_creditmemo.get('DocumentStatus', '') or ''
        comments = str(api_creditmemo.get('Comments', '')).strip() if api_creditmemo.get('Comments') else ''
        rounding_diff_amount = api_creditmemo.get('RoundingDiffAmount', 0) or 0
        
        # Calculate doc_total_without_vat (Subtotal AFTER discount)
        # RoundingDiffAmount (if negative like -10.4) should be SUBTRACTED from doc_total_without_vat
        # Formula: doc_total_without_vat = DocTotal - VATSum - RoundingDiffAmount
        # Example: DocTotal - VATSum - (-10.4) = DocTotal - VATSum + 10.4 (gets actual subtotal)
        doc_total_without_vat = doc_total - vat_sum
        if rounding_diff_amount:
            doc_total_without_vat = doc_total_without_vat - rounding_diff_amount
        
        # AR Credit Memo: All credit memos are negative by default (they're credits/reductions)
        # BUT if cancel_status is 'csCancellation', make them positive (reversing the credit)
        # So: if cancel_status != 'csCancellation', multiply by -1 (make negative)
        #     if cancel_status == 'csCancellation', keep positive (multiply by 1)
        sign_multiplier = 1 if cancel_status == 'csCancellation' else -1
        
        # Apply sign to header amounts
        doc_total = doc_total * sign_multiplier
        doc_total_without_vat = doc_total_without_vat * sign_multiplier
        vat_sum = vat_sum * sign_multiplier
        total_discount = total_discount * sign_multiplier
        # Also apply sign to rounding_diff_amount for consistency
        rounding_diff_amount = rounding_diff_amount * sign_multiplier
        
        # Calculate subtotal_before_discount (Subtotal BEFORE discount)
        # This is the subtotal before discount is applied (after sign multiplier)
        subtotal_before_discount = doc_total_without_vat + total_discount
        
        # Map document lines
        document_lines = api_creditmemo.get('DocumentLines', [])
        items = []
        
        for idx, line in enumerate(document_lines):
            item_code = str(line.get('ItemCode', '')) if line.get('ItemCode') else ''
            item_description = line.get('ItemDescription', '') or ''
            upc_code = line.get('U_UPCCODE', '') or ''
            
            # Ensure item exists in Items table
            item_obj = self._ensure_item_exists(item_code, item_description, upc_code)
            
            # Line number (0-based in API, convert to 1-based)
            line_num = line.get('LineNum', idx)
            if isinstance(line_num, int):
                line_no = line_num + 1 if line_num >= 0 else idx + 1
            else:
                line_no = idx + 1
            
            quantity = line.get('Quantity', 0) or 0
            price = line.get('Price', 0) or 0
            price_after_vat = line.get('PriceAfterVAT', 0) or 0
            discount_percent_line = self._clamp_percentage(line.get('DiscountPercent', 0) or 0, 'discount_percent_line')
            line_total = line.get('LineTotal', 0) or 0
            cost_price = line.get('GrossProfitTotalBasePrice', 0) or 0  # Total cost price for this line
            tax_percentage = self._clamp_percentage(line.get('TaxPercentagePerRow', 0) or 0, 'tax_percentage')
            tax_total = line.get('TaxTotal', 0) or 0
            
            # Calculate line_total_after_discount if header discount exists
            # Discount is applied at header level, but we show line_total_after_discount for viewing
            line_total_after_discount = line_total
            if discount_percent and discount_percent > 0:
                line_total_after_discount = line_total * (1 - discount_percent / 100)
            
            # Calculate Gross Profit = LineTotal after discount - cost_price
            gross_profit = line_total_after_discount - cost_price
            
            # Apply sign multiplier to item amounts
            quantity = quantity * sign_multiplier # Quantity also needs to be signed for credit memos
            price = price * sign_multiplier
            price_after_vat = price_after_vat * sign_multiplier
            line_total = line_total * sign_multiplier
            line_total_after_discount = line_total_after_discount * sign_multiplier
            cost_price = cost_price * sign_multiplier
            gross_profit = gross_profit * sign_multiplier
            tax_total = tax_total * sign_multiplier
            
            item_data = {
                'line_no': line_no,
                'item_code': item_code,
                'item_description': item_description,
                'quantity': quantity,
                'price': price,
                'price_after_vat': price_after_vat,
                'discount_percent': discount_percent_line,
                'line_total': line_total,
                'line_total_after_discount': line_total_after_discount,
                'cost_price': cost_price,
                'gross_profit': gross_profit,
                'tax_percentage': tax_percentage,
                'tax_total': tax_total,
                'upc_code': upc_code,
                'item_id': item_obj.id if item_obj else None,  # For ForeignKey
            }
            
            items.append(item_data)
        
        # Calculate total gross profit (sum of all item gross_profit)
        total_gross_profit = sum(item.get('gross_profit', 0) or 0 for item in items)
        
        return {
            'credit_memo_number': docnum,
            'internal_number': doc_entry,
            'posting_date': posting_date,
            'doc_due_date': doc_due_date,
            'customer_code': customer_code or '',
            'customer_name': customer_name or '',
            'customer_address': customer_address,
            'salesman_name': salesman_name or '',
            'salesman_code': salesman_code,
            'store': store,
            'bp_reference_no': bp_reference or '',
            'doc_total': doc_total,
            'doc_total_without_vat': doc_total_without_vat,
            'subtotal_before_discount': subtotal_before_discount,
            'vat_sum': vat_sum,
            'total_gross_profit': total_gross_profit,
            'discount_percent': discount_percent,
            'cancel_status': cancel_status,
            'document_status': document_status,
            'vat_number': vat_number,
            'comments': comments,
            'rounding_diff_amount': rounding_diff_amount,
            'items': items,
        }
    
    def fetch_open_quotations_last_pages(self, last_pages: int = 15) -> List[Dict]:
        """
        Fetch open quotations, but only fetch the last N pages (skip earlier pages)
        
        Args:
            last_pages: Number of last pages to fetch (default: 15)
        
        Returns:
            List of open quotations from the last N pages
        """
        base_url = getattr(settings, 'SAP_QUOTATION_API_URL', 'http://192.168.1.103/IntegrationApi/api/SalesQuotations')
        payload = {"DocumentStatus": "bost_Open"}
        records_per_page = 20
        
        logger.info(f"Fetching last {last_pages} pages of open quotations...")
        
        # Fetch first page to get total count
        first_page = self._make_request_with_url(payload, base_url, page_number=1)
        if first_page is None:
            return []
        
        total_count = first_page.get('count', 0)
        if total_count == 0:
            logger.info("No open quotations found")
            return []
        
        # Calculate total pages and which pages to fetch
        total_pages = (total_count + records_per_page - 1) // records_per_page  # Ceiling division
        start_page = max(1, total_pages - last_pages + 1)
        
        logger.info(f"Total records: {total_count}, total pages: {total_pages}, fetching pages {start_page} to {total_pages}")
        
        all_quotations = []
        
        # Fetch the last N pages
        for page_num in range(start_page, total_pages + 1):
            logger.info(f"  Fetching page {page_num}/{total_pages}...")
            page_result = self._make_request_with_url(payload, base_url, page_number=page_num)
            if page_result is None:
                logger.warning(f"  Failed to fetch page {page_num}, continuing...")
                continue
            
            page_quotations = page_result.get('value', [])
            all_quotations.extend(page_quotations)
            logger.info(f"  ✓ Fetched page {page_num}/{total_pages}: {len(page_quotations)} quotations")
        
        logger.info(f"Fetched {len(all_quotations)} open quotations from last {last_pages} pages")
        return all_quotations
    
    def fetch_quotations_by_date_range(self, from_date: str, to_date: str) -> List[Dict]:
        """
        Fetch quotations for a date range (with pagination)
        
        Args:
            from_date: Start date in YYYY-MM-DD format
            to_date: End date in YYYY-MM-DD format
        
        Returns:
            List of quotations for that date range from all pages
        """
        payload = {"FromDate": from_date, "ToDate": to_date}
        base_url = getattr(settings, 'SAP_QUOTATION_API_URL', 'http://192.168.1.103/IntegrationApi/api/SalesQuotations')
        logger.info(f"Fetching quotations for date range: {from_date} to {to_date} (with pagination)...")
        all_quotations = self._fetch_all_pages_with_url(payload, base_url)
        logger.info(f"Fetched {len(all_quotations)} quotations for {from_date} to {to_date} (all pages)")
        return all_quotations
    
    def fetch_quotations_last_n_days(self, days: int = 3) -> List[Dict]:
        """
        Fetch quotations for the last N days (one call per day range)
        
        Args:
            days: Number of days to go back (default: 3)
        
        Returns:
            Combined list of all quotations from the last N days (deduplicated by DocNum)
        """
        all_quotations = []
        seen_docnums = set()
        
        # Calculate date range: from (today - days) to today
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days-1)  # Include today + last N-1 days
        
        from_date_str = start_date.strftime('%Y-%m-%d')
        to_date_str = end_date.strftime('%Y-%m-%d')
        
        logger.info(f"Fetching quotations for last {days} days: {from_date_str} to {to_date_str}")
        quotations = self.fetch_quotations_by_date_range(from_date_str, to_date_str)
        
        # Deduplicate by DocNum
        for quotation in quotations:
            docnum = quotation.get('DocNum')
            if docnum and docnum not in seen_docnums:
                all_quotations.append(quotation)
                seen_docnums.add(docnum)
        
        logger.info(f"Total unique quotations from last {days} days: {len(all_quotations)}")
        return all_quotations
    
    def sync_all_quotations(self, days_back: int = 3) -> List[Dict]:
        """
        Main sync method: Fetch open quotations (last 15 pages) + new quotations from last N days
        
        Args:
            days_back: Number of days to fetch for new quotations (default: 3, i.e., today + last 2 days = 3 days total)
        
        Returns:
            Combined and deduplicated list of all quotations
        """
        all_quotations = []
        seen_docnums = set()
        
        # Step 1: Fetch open quotations (last 15 pages only)
        logger.info("Step 1: Fetching open quotations (last 15 pages)...")
        open_quotations = self.fetch_open_quotations_last_pages(last_pages=15)
        for quotation in open_quotations:
            docnum = quotation.get('DocNum')
            if docnum:
                all_quotations.append(quotation)
                seen_docnums.add(docnum)
        
        # Step 2: Fetch new quotations from last N days
        logger.info(f"Step 2: Fetching new quotations from last {days_back} days...")
        new_quotations = self.fetch_quotations_last_n_days(days_back)
        for quotation in new_quotations:
            docnum = quotation.get('DocNum')
            if docnum and docnum not in seen_docnums:
                all_quotations.append(quotation)
                seen_docnums.add(docnum)
        
        logger.info(f"Total unique quotations after sync: {len(all_quotations)}")
        return all_quotations
    
    def _map_quotation_api_response_to_model(self, api_quotation: Dict) -> Dict:
        """
        Map API response to Django model format for Quotation
        
        Args:
            api_quotation: Single quotation from API response
        
        Returns:
            Dictionary with mapped fields for SAPQuotation and SAPQuotationItem
        """
        # Extract header fields
        docnum = str(api_quotation.get('DocNum', ''))
        doc_entry = str(api_quotation.get('DocEntry', '')) if api_quotation.get('DocEntry') else None
        
        # Date parsing
        doc_date_str = api_quotation.get('DocDate', '')
        posting_date = None
        if doc_date_str:
            try:
                posting_date = datetime.strptime(doc_date_str, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                try:
                    posting_date = datetime.strptime(doc_date_str, '%Y/%m/%d').date()
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse DocDate: {doc_date_str}")
        
        # Business Partner
        bp = api_quotation.get('BusinessPartner', {})
        customer_code = bp.get('CardCode', '') or api_quotation.get('CardCode', '')
        customer_name = bp.get('CardName', '') or api_quotation.get('CardName', '')
        
        # Sales Person
        sales_person = api_quotation.get('SalesPerson', {})
        salesman_name = sales_person.get('SalesEmployeeName', '') or ''
        
        # Other header fields
        bp_reference = api_quotation.get('NumAtCard', '') or ''
        bill_to = str(api_quotation.get('Address', '')).strip() if api_quotation.get('Address') else ''
        comments = str(api_quotation.get('Comments', '')).strip() if api_quotation.get('Comments') else ''
        
        # Financial fields
        doc_total = api_quotation.get('DocTotal', 0) or 0
        vat_sum = api_quotation.get('VatSum', 0) or 0
        total_discount = api_quotation.get('TotalDiscount', 0) or 0
        rounding_diff_amount = api_quotation.get('RoundingDiffAmount', 0) or 0
        discount_percent = self._clamp_percentage(api_quotation.get('DiscountPercent', 0) or 0, 'discount_percent')
        
        # Calculate document_total as Subtotal without VAT
        # Formula: document_total = DocTotal - VatSum - RoundingDiffAmount - TotalDiscount
        document_total = doc_total - vat_sum - rounding_diff_amount - total_discount
        
        # Document Status mapping - unified to OPEN/CLOSED
        doc_status = api_quotation.get('DocumentStatus', '')
        status = "OPEN" if doc_status == "bost_Open" else "CLOSED"
        
        # Map document lines
        document_lines = api_quotation.get('DocumentLines', [])
        items = []
        
        for idx, line in enumerate(document_lines):
            item_code = str(line.get('ItemCode', '')) if line.get('ItemCode') else ''
            item_description = line.get('ItemDescription', '') or ''
            quantity = line.get('Quantity', 0) or 0
            price = line.get('Price', 0) or 0
            line_total = line.get('LineTotal', 0) or 0
            
            item_data = {
                'item_no': item_code,
                'description': item_description,
                'quantity': quantity,
                'price': price,
                'row_total': line_total,
            }
            
            items.append(item_data)
        
        return {
            'q_number': docnum,
            'internal_number': doc_entry,
            'posting_date': posting_date,
            'customer_code': customer_code or '',
            'customer_name': customer_name or '',
            'salesman_name': salesman_name or '',
            'bp_reference_no': bp_reference or '',
            'document_total': document_total,  # Subtotal without VAT
            'vat_sum': vat_sum,
            'total_discount': total_discount,
            'rounding_diff_amount': rounding_diff_amount,
            'discount_percent': discount_percent,  # Optional, but store it
            'status': status,
            'bill_to': bill_to,
            'remarks': comments,
            'items': items,
        }
    
    def fetch_finance_summary(self) -> List[Dict]:
        """
        Fetch customer finance summary data from FinanceSummary API endpoint
        
        Returns:
            List of customer finance records from API
        """
        base_url = getattr(settings, 'SAP_FINANCE_SUMMARY_API_URL', 'http://192.168.1.103/IntegrationApi/api/FinanceSummary')
        logger.info("Fetching customer finance summary from API...")
        
        try:
            response = requests.get(
                base_url,
                timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
            
            # API returns data in format: {"Count": 4499, "Data": [...]}
            if isinstance(data, dict):
                finance_data = data.get('Data', [])
                count = data.get('Count', len(finance_data))
                logger.info(f"Fetched {len(finance_data)} customer finance records (Total: {count})")
                return finance_data
            elif isinstance(data, list):
                logger.info(f"Fetched {len(data)} customer finance records")
                return data
            else:
                logger.warning(f"Unexpected API response format: {type(data)}")
                return []
                
        except requests.exceptions.Timeout:
            logger.error(f"API request timeout after {self.timeout}s: {base_url}")
            raise RuntimeError(f"SAP API timeout ({self.timeout}s). Check if SSH tunnel is connected.") from None
        except requests.exceptions.ConnectionError as e:
            logger.error(f"API connection error: {e}, url: {base_url}")
            raise RuntimeError("Cannot connect to SAP API. Is the SSH tunnel running? (ssh -N -R 8443:192.168.1.103:80 root@VPS)") from None
        except requests.exceptions.RequestException as e:
            logger.error(f"API request error: {e}, url: {base_url}")
            raise RuntimeError(f"SAP API error: {e}") from None
        except Exception as e:
            logger.error(f"Unexpected error in finance summary API request: {e}, url: {base_url}")
            return []