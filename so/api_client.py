"""
SAP API Client for fetching Sales Orders
"""
import requests
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from django.conf import settings
from so.models import Items

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
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"API request error: {e}, payload: {payload}, page: {page_number}")
            return None
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
        
        # Step 3: Preload all manufacturers and stock for all orders (batch optimization)
        logger.info("Step 3: Preloading manufacturers and stock for all items...")
        all_item_codes = set()
        for order in all_orders:
            for line in order.get('DocumentLines', []):
                item_code = line.get('ItemCode')
                if item_code:
                    all_item_codes.add(str(item_code))
        
        if all_item_codes:
            self._load_manufacturer_cache(list(all_item_codes))
            self._load_stock_cache(list(all_item_codes))
            logger.info(f"Preloaded manufacturers and stock for {len(all_item_codes)} unique items")
        
        return all_orders
    
    def _filter_ho_customers(self, orders: List[Dict]) -> List[Dict]:
        """
        Filter orders to only include those where CardCode starts with "HO"
        
        Args:
            orders: List of sales order dictionaries
        
        Returns:
            Filtered list of orders
        """
        filtered = []
        for order in orders:
            card_code = order.get('CardCode', '') or order.get('BusinessPartner', {}).get('CardCode', '')
            if isinstance(card_code, str) and card_code.strip().upper().startswith('HO'):
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
                self._stock_cache[item.item_code] = {
                    'total_available_stock': item.total_available_stock or 0,
                    'dip_warehouse_stock': item.dip_warehouse_stock or 0,
                }
            # Cache misses (items not found)
            for code in uncached_codes:
                if code not in self._stock_cache:
                    self._stock_cache[code] = {
                        'total_available_stock': 0,
                        'dip_warehouse_stock': 0,
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
            return {'total_available_stock': 0, 'dip_warehouse_stock': 0}
        
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
            stock_data = {'total_available_stock': 0, 'dip_warehouse_stock': 0}
            self._stock_cache[item_code] = stock_data
            return stock_data
        except Exception as e:
            logger.warning(f"Error looking up stock for item {item_code}: {e}")
            return {'total_available_stock': 0, 'dip_warehouse_stock': 0}
    
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
        
        # Batch load manufacturers and stock for all items in this order (optimization)
        item_codes = [str(line.get('ItemCode', '')) for line in document_lines if line.get('ItemCode')]
        if item_codes:
            self._load_manufacturer_cache(item_codes)
            self._load_stock_cache(item_codes)
        
        for idx, line in enumerate(document_lines):
            item_code = str(line.get('ItemCode', '')) if line.get('ItemCode') else ''
            
            # Lookup manufacturer from cache (already loaded above)
            manufacture = self._get_manufacturer_from_item_code(item_code)
            
            # Lookup stock from cache (already loaded above)
            stock_data = self._get_stock_from_item_code(item_code)
            
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
                'total_available_stock': stock_data.get('total_available_stock', 0),  # From Items model
                'dip_warehouse_stock': stock_data.get('dip_warehouse_stock', 0),  # From Items model
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
            'is_sap_pi': is_sap_pi,  # True if U_PROFORMAINVOICE=Y
            'sap_pi_lpo_date': sap_pi_lpo_date,  # From API field U_Lpdate (date or None)
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
