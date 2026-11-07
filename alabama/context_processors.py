from django.conf import settings


def alabama_context(request):
    """Add is_alabama_admin, is_alabama_salesman, is_manager for Alabama base template.

    Also `show_item_catalog` (Alabama sidebar link) and `alabama_catalog_link` (so / manager link on any page): the Alabama
    Item Catalog exists only while settings.ALABAMA_OWN_CATALOG is on, so the links are offered only then."""
    catalog_on = bool(getattr(settings, 'ALABAMA_OWN_CATALOG', False))
    user = request.user
    uname = (user.username or '').strip().lower() if user.is_authenticated else ''
    extra = {'alabama_catalog_link': catalog_on and uname in ('so', 'manager')}
    if not request.path.startswith('/alabama/'):
        return extra
    if not user.is_authenticated:
        return dict(extra, is_alabama_admin=False, is_alabama_salesman=False, is_manager=False, show_item_catalog=False)
    # manager sees everything (View Sales Orders, View Quotations, Back to Junaid, etc.)
    if uname == 'manager':
        return dict(extra, is_alabama_admin=True, is_alabama_salesman=False, is_manager=True, show_item_catalog=catalog_on)
    role = getattr(user, 'role', None)
    if not role:
        return dict(extra, is_alabama_admin=False, is_alabama_salesman=False, is_manager=False, show_item_catalog=False)
    company = getattr(role, 'company', 'Junaid')
    is_alabama_admin = role.role == 'Admin' and company == 'Alabama'
    is_alabama_salesman = role.role == 'Salesman' and company == 'Alabama'
    # Item Catalog link: so (works for both companies), Alabama admins and Alabama salesmen
    show = catalog_on and (is_alabama_admin or is_alabama_salesman or (uname == 'so' and role.role == 'Admin'))
    return dict(extra, is_alabama_admin=is_alabama_admin, is_alabama_salesman=is_alabama_salesman, is_manager=False,
                show_item_catalog=show)
