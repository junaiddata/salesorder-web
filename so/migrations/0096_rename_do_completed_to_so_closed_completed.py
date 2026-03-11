# Rename approval_status 'DO Completed' to 'SO Closed/Completed'

from django.db import migrations


def update_approval_status(apps, schema_editor):
    SAPSalesorder = apps.get_model('so', 'SAPSalesorder')
    SAPSalesorder.objects.filter(approval_status='DO Completed').update(approval_status='SO Closed/Completed')


def reverse_update(apps, schema_editor):
    SAPSalesorder = apps.get_model('so', 'SAPSalesorder')
    SAPSalesorder.objects.filter(approval_status='SO Closed/Completed').update(approval_status='DO Completed')


class Migration(migrations.Migration):

    dependencies = [
        ('so', '0095_add_revised_price'),
    ]

    operations = [
        migrations.RunPython(update_approval_status, reverse_update),
    ]
