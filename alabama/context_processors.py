def alabama_context(request):
    """Add is_alabama_admin, is_alabama_salesman, is_manager for Alabama base template."""
    if not request.path.startswith('/alabama/'):
        return {}
    if not request.user.is_authenticated:
        return {'is_alabama_admin': False, 'is_alabama_salesman': False, 'is_manager': False}
    # manager sees everything (View Sales Orders, View Quotations, Back to Junaid, etc.)
    if (request.user.username or '').strip().lower() == 'manager':
        return {'is_alabama_admin': True, 'is_alabama_salesman': False, 'is_manager': True}
    role = getattr(request.user, 'role', None)
    if not role:
        return {'is_alabama_admin': False, 'is_alabama_salesman': False, 'is_manager': False}
    company = getattr(role, 'company', 'Junaid')
    is_alabama_admin = role.role == 'Admin' and company == 'Alabama'
    is_alabama_salesman = role.role == 'Salesman' and company == 'Alabama'
    return {'is_alabama_admin': is_alabama_admin, 'is_alabama_salesman': is_alabama_salesman, 'is_manager': False}
