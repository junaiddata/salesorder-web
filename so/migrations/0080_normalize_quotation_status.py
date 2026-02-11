# Generated migration to normalize quotation status values

from django.db import migrations


def normalize_status_forward(apps, schema_editor):
    """Normalize status values: O -> OPEN, C -> CLOSED"""
    SAPQuotation = apps.get_model('so', 'SAPQuotation')
    
    # Update O -> OPEN
    SAPQuotation.objects.filter(status__in=['O', 'o']).update(status='OPEN')
    
    # Update C -> CLOSED
    SAPQuotation.objects.filter(status__in=['C', 'c']).update(status='CLOSED')
    
    # Update Open -> OPEN (case variations)
    SAPQuotation.objects.filter(status__iexact='Open').update(status='OPEN')
    
    # Update Closed -> CLOSED (case variations)
    SAPQuotation.objects.filter(status__iexact='Closed').update(status='CLOSED')


def normalize_status_reverse(apps, schema_editor):
    """Reverse: OPEN -> O, CLOSED -> C"""
    SAPQuotation = apps.get_model('so', 'SAPQuotation')
    
    SAPQuotation.objects.filter(status='OPEN').update(status='O')
    SAPQuotation.objects.filter(status='CLOSED').update(status='C')


class Migration(migrations.Migration):

    dependencies = [
        ('so', '0079_sapquotation_vat_discount_rounding_fields'),
    ]

    operations = [
        migrations.RunPython(normalize_status_forward, normalize_status_reverse),
    ]
