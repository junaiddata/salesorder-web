"""
Alabama Delivery Order views - header + detail model.
Uses salesman mappings from Settings for Sales Person field.
"""
import pandas as pd
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q, Count, Sum
from django.core.paginator import Paginator
from datetime import datetime
from decimal import Decimal

from .models import AlabamaDeliveryOrder, AlabamaDeliveryOrderItem
from .views import alabama_salesman_scope_q, normalize_alabama_salesman


def _col_map(df):
    col_map = {}
    aliases = {
        'do': ['do', 'do number', 'donumber'],
        'date': ['date'],
        'customer_code': ['customer code', 'customercode'],
        'customer': ['customer', 'customer name', 'customername'],
        'sales_person': ['sales person', 'salesperson', 'sales man', 'salesman'],
        'city': ['city'],
        'area': ['area'],
        'lpo': ['lpo'],
        'remarks': ['remarks'],
        'invoice': ['invoice'],
        'amount': ['amount'],
        'item_no': ['item no', 'itemno', 'item no.'],
        'item_description': ['item/service description', 'item description', 'itemdescription'],
        'quantity': ['quantity', 'qty'],
        'price': ['price'],
    }
    for col in df.columns:
        c = str(col).strip().lower().replace('\ufeff', '').replace('\xa0', ' ')
        for canonical, alis in aliases.items():
            if c == canonical or c in alis:
                col_map[col] = canonical
                break
    return col_map


def _parse_date(val):
    if pd.isna(val):
        return None
    if hasattr(val, 'date'):
        return val.date()
    if isinstance(val, (int, float)):
        try:
            return pd.Timestamp(val).date()
        except Exception:
            pass
    s = str(val).strip()
    for fmt in ['%d.%m.%y', '%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%d-%m-%y', '%Y%m%d']:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_decimal(x):
    if pd.isna(x):
        return Decimal('0')
    try:
        return Decimal(str(x).replace(',', '').strip())
    except Exception:
        return Decimal('0')


def _to_str(x):
    if pd.isna(x):
        return ''
    s = str(x).strip()
    if s.endswith('.0') and s.replace('.', '').isdigit():
        s = s[:-2]
    return s


@login_required
def delivery_order_upload(request):
    """Upload Excel file for Alabama Delivery Orders. Uses salesman mappings for Sales Person."""
    from so.models import Customer, Items
    from django.db import transaction

    if request.method == 'POST':
        excel_file = request.FILES.get('excel_file')
        if not excel_file:
            messages.error(request, 'Please upload an Excel file.')
            return render(request, 'alabama/delivery_order_upload.html', {'active_page': 'delivery_order_upload'})

        try:
            df = pd.read_excel(excel_file)
            df.columns = [str(c).strip().replace('\ufeff', '').replace('\xa0', ' ') for c in df.columns]
            col_map = _col_map(df)
            required = ['do', 'date', 'customer_code', 'customer', 'sales_person', 'invoice', 'amount',
                        'item_no', 'item_description', 'quantity', 'price']
            missing = [r for r in required if r not in col_map.values()]
            if missing:
                return render(request, 'alabama/delivery_order_upload.html', {
                    'error': f'Missing columns: {", ".join(missing)}. Expected: DO, DATE, CUSTOMER CODE, CUSTOMER, Sales Person, CITY, AREA, LPO, Remarks, INVOICE, AMOUNT, Item No., Item/Service Description, Quantity, Price',
                    'active_page': 'delivery_order_upload',
                })

            rev_map = {v: k for k, v in col_map.items() if v in required}
            rev_map = {k: rev_map[k] for k in required if k in rev_map}
            optional = ['city', 'area', 'lpo', 'remarks']
            for o in optional:
                if o in col_map.values():
                    rev_map[o] = next(k for k, v in col_map.items() if v == o)

            def get_val(row, key):
                col = rev_map.get(key)
                if col is None:
                    return ''
                return row.get(col, '')

            # Group rows by DO number to build header + items
            from collections import defaultdict
            do_data = defaultdict(lambda: {'header': None, 'items': []})

            for idx, row in df.iterrows():
                do_no = _to_str(get_val(row, 'do'))
                if not do_no:
                    continue
                dt = _parse_date(get_val(row, 'date'))
                if not dt:
                    continue
                cust_code = _to_str(get_val(row, 'customer_code'))
                cust_name = _to_str(get_val(row, 'customer'))
                if not cust_code or not cust_name:
                    continue
                sales_person = normalize_alabama_salesman(_to_str(get_val(row, 'sales_person')))
                item_no = _to_str(get_val(row, 'item_no'))
                if not item_no:
                    continue
                item_desc = _to_str(get_val(row, 'item_description'))
                qty = _to_decimal(get_val(row, 'quantity'))
                price_val = _to_decimal(get_val(row, 'price'))
                amount_val = _to_decimal(get_val(row, 'amount'))

                if do_data[do_no]['header'] is None:
                    customer, _ = Customer.objects.get_or_create(
                        customer_code=cust_code,
                        defaults={'customer_name': cust_name}
                    )
                    do_data[do_no]['header'] = {
                        'do_number': do_no,
                        'date': dt,
                        'customer': customer,
                        'sales_person': sales_person or None,
                        'city': _to_str(get_val(row, 'city')) or None,
                        'area': _to_str(get_val(row, 'area')) or None,
                        'lpo': _to_str(get_val(row, 'lpo')) or None,
                        'remarks': _to_str(get_val(row, 'remarks')) or None,
                        'invoice': _to_str(get_val(row, 'invoice')) or None,
                    }

                item, _ = Items.objects.get_or_create(
                    item_code=item_no,
                    defaults={'item_description': item_desc or item_no}
                )
                do_data[do_no]['items'].append({
                    'item': item,
                    'item_description': item_desc or None,
                    'quantity': qty,
                    'price': price_val,
                    'amount': amount_val,
                })

            if not do_data:
                return render(request, 'alabama/delivery_order_upload.html', {
                    'error': f'No valid rows found. Check dates, DO, Customer Code, Customer, Item No. Found {len(df)} rows.',
                    'active_page': 'delivery_order_upload',
                })

            total_items = sum(len(d['items']) for d in do_data.values())
            with transaction.atomic():
                for do_no in do_data:
                    AlabamaDeliveryOrder.objects.filter(do_number=do_no).delete()
                for do_no, data in do_data.items():
                    do = AlabamaDeliveryOrder.objects.create(**data['header'])
                    AlabamaDeliveryOrderItem.objects.bulk_create([
                        AlabamaDeliveryOrderItem(delivery_order=do, **item)
                        for item in data['items']
                    ])

            messages.success(
                request,
                f'Successfully uploaded {total_items} line items from {len(do_data)} delivery orders.'
            )
            return redirect('alabama:settings')

        except Exception as e:
            messages.error(request, f'Upload failed: {str(e)}')
            return render(request, 'alabama/delivery_order_upload.html', {'error': str(e), 'active_page': 'delivery_order_upload'})

    return render(request, 'alabama/delivery_order_upload.html', {'active_page': 'delivery_order_upload'})


@login_required
def delivery_order_list(request):
    """List Alabama Delivery Orders (header + items)."""
    qs = AlabamaDeliveryOrder.objects.all().select_related('customer').annotate(
        items_count=Count('items'),
        total_amount=Sum('items__amount'),
    )

    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        scope_q = alabama_salesman_scope_q(request.user, field='sales_person')
        qs = qs.filter(scope_q)

    q = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    page_size = int(request.GET.get('page_size', 25)) or 25
    page_size = max(5, min(100, page_size))

    if salesmen_filter:
        clean = [s for s in salesmen_filter if s.strip()]
        if clean:
            qs = qs.filter(sales_person__in=clean)

    def parse_date(s):
        if not s:
            return None
        try:
            if len(s) == 7:
                return datetime.strptime(s + '-01', '%Y-%m-%d').date()
            return datetime.strptime(s, '%Y-%m-%d').date()
        except ValueError:
            return None

    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date:
        qs = qs.filter(date__gte=start_date)
    if end_date:
        qs = qs.filter(date__lte=end_date)

    if q:
        qs = qs.filter(
            Q(do_number__icontains=q) |
            Q(customer__customer_name__icontains=q) |
            Q(customer__customer_code__icontains=q) |
            Q(sales_person__icontains=q) |
            Q(invoice__icontains=q)
        )

    salesmen = list(AlabamaDeliveryOrder.objects.values_list('sales_person', flat=True).distinct().order_by('sales_person'))
    salesmen = [s for s in salesmen if s]

    qs = qs.order_by('-date', '-do_number')
    paginator = Paginator(qs, page_size)
    page_num = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_num)
    except Exception:
        page_obj = paginator.page(1)

    query_params = []
    if q:
        query_params.append(f'q={q}')
    for s in salesmen_filter:
        if s:
            query_params.append(f'salesman={s}')
    if start:
        query_params.append(f'start={start}')
    if end:
        query_params.append(f'end={end}')
    if page_size != 25:
        query_params.append(f'page_size={page_size}')
    query_string = '&'.join(query_params)

    return render(request, 'alabama/delivery_order_list.html', {
        'page_obj': page_obj,
        'total_count': paginator.count,
        'salesmen': salesmen,
        'filters': {
            'q': q,
            'salesmen_filter': salesmen_filter,
            'start': start,
            'end': end,
            'page_size': page_size,
        },
        'query_string': query_string,
        'active_page': 'delivery_order',
    })


@login_required
def delivery_order_detail(request, do_number):
    """Detail view for a single Delivery Order (header + line items)."""
    from django.http import Http404

    do = AlabamaDeliveryOrder.objects.filter(do_number=do_number).select_related('customer').prefetch_related('items__item').first()
    if not do:
        raise Http404("Delivery order not found")

    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        scope_q = alabama_salesman_scope_q(request.user, field='sales_person')
        if not AlabamaDeliveryOrder.objects.filter(pk=do.pk).filter(scope_q).exists():
            raise Http404("Delivery order not found")

    items = list(do.items.all().select_related('item').order_by('id'))
    total_amount = sum(float(i.amount) for i in items)

    return render(request, 'alabama/delivery_order_detail.html', {
        'do': do,
        'items': items,
        'total_amount': total_amount,
        'active_page': 'delivery_order',
    })
