def alabama_context(request):
    """Add is_alabama_admin, is_alabama_salesman for Alabama base template."""
    if not request.path.startswith('/alabama/'):
        return {}
    if not request.user.is_authenticated:
        return {'is_alabama_admin': False, 'is_alabama_salesman': False}
    # manager sees everything (View Sales Orders, View Quotations, etc.)
    if (request.user.username or '').strip().lower() == 'manager':
        return {'is_alabama_admin': True, 'is_alabama_salesman': False}
    role = getattr(request.user, 'role', None)
    if not role:
        return {'is_alabama_admin': False, 'is_alabama_salesman': False}
    company = getattr(role, 'company', 'Junaid')
    is_alabama_admin = role.role == 'Admin' and company == 'Alabama'
    is_alabama_salesman = role.role == 'Salesman' and company == 'Alabama'
    return {'is_alabama_admin': is_alabama_admin, 'is_alabama_salesman': is_alabama_salesman}
