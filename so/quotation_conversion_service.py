"""Reusable "Quotation -> Sales Order" conversion logic -- the single
implementation of how a Quotation becomes a real SalesOrder, shared by the
manual "Convert to Sales Order" button (so/views_quotation.py) and the LPO
agent (emailagent/lpo_agent.py), which auto-creates a Sales Order when a
client's own Purchase Order confidently matches an already-Approved
quotation. Keeping this in exactly one place means both callers always
behave identically -- there is no separate/duplicate conversion path
anywhere else in the app.

Deliberately request-independent: raises ValueError on any failure rather
than using django.contrib.messages/redirect, so it's callable from
non-request code (the LPO agent, management commands) as well as from a view.
"""
from .models import CustomerPrice, OrderItem, SalesOrder


def check_conversion_eligibility(quotation):
    """Same 4 gates, same order, same wording as the manual "Convert to
    Sales Order" button always used -- shared so a human sees the exact
    same reason whether they hit the button or the LPO agent's review page.
    Returns (eligible: bool, reason: str) -- reason is '' when eligible."""
    if quotation.converted_to_sales_order:
        return False, f'Already converted to Sales Order {quotation.converted_to_sales_order.order_number}.'
    if quotation.status != 'Approved':
        return False, 'Quotation must be approved before it can be converted to a sales order.'
    if quotation.discount_approval_status not in ('NOT_REQUIRED', 'APPROVED'):
        return False, 'This quotation has a discount pending manager approval. It must be approved (or removed) before converting to a sales order.'
    if not quotation.items.exists():
        return False, 'Cannot convert quotation with no items.'
    return True, ''


def convert_quotation_to_sales_order(quotation, *, username=None, created_via=SalesOrder.CREATED_VIA_MANUAL):
    """Creates a real SalesOrder (+ OrderItems, + any CustomerPrice
    updates) from `quotation`'s line items -- identical logic to what the
    manual "Convert to Sales Order" view has always done, just moved here
    so it has exactly one implementation. `username` replaces
    request.user.username for the ALABAMA-division fallback heuristic when
    quotation.division is blank; pass None (e.g. from the LPO agent, which
    has no request/user) to skip that heuristic and fall back straight to
    'JUNAID'. `created_via` is stamped onto the created SalesOrder so it
    stays identifiable afterward (see SalesOrder.created_via) -- leave it
    at the default 'manual' for any human-triggered call (the quotation
    page's button, or a human picking a candidate on the LPO review page);
    only emailagent.lpo_agent's fully-automatic match passes 'agent_lpo'.

    Raises ValueError (never django.contrib.messages) when `quotation`
    isn't eligible (see check_conversion_eligibility) or ends up with zero
    convertible (item-linked) QuotationItems. Caller is responsible for
    wrapping this in its own transaction if it needs to be atomic with
    other work (see how emailagent.lpo_agent and the view do this)."""
    eligible, reason = check_conversion_eligibility(quotation)
    if not eligible:
        raise ValueError(reason)

    division = quotation.division
    if not division:
        division = 'JUNAID'
        if username and 'alabama' in username.lower():
            division = 'ALABAMA'

    sales_order = SalesOrder.objects.create(
        customer=quotation.customer,
        division=division,
        salesman=quotation.salesman,
        remarks=quotation.remarks or '',
        created_via=created_via,
    )

    order_items = []
    customer_price_updates = []
    total_amount = 0.0

    for quotation_item in quotation.items.all():
        if not quotation_item.item:
            continue  # Skip items without valid item reference

        item = quotation_item.item
        quantity = quotation_item.quantity
        price = quotation_item.price
        unit = quotation_item.unit if quotation_item.unit in ['pcs', 'ctn', 'roll'] else 'pcs'

        is_custom_price = abs(float(price) - float(item.item_price)) > 0.01
        line_total = quantity * price
        total_amount += line_total

        order_items.append(OrderItem(
            order=sales_order,
            item=item,
            quantity=quantity,
            price=price,
            unit=unit,
            is_custom_price=is_custom_price,
        ))

        if is_custom_price:
            customer_price_updates.append((quotation.customer, item, price))

    if not order_items:
        sales_order.delete()
        raise ValueError('No valid items found in quotation to convert.')

    OrderItem.objects.bulk_create(order_items)

    for customer, item, price in customer_price_updates:
        CustomerPrice.objects.update_or_create(
            customer=customer,
            item=item,
            defaults={'custom_price': price},
        )

    # Carry over the approved quotation discount, if any.
    net_amount = max(total_amount - quotation.discount_amount, 0.0)
    tax = round(0.05 * net_amount, 2)
    sales_order.tax = tax
    sales_order.total_amount = net_amount
    sales_order.save()

    quotation.converted_to_sales_order = sales_order
    quotation.save()

    return sales_order
