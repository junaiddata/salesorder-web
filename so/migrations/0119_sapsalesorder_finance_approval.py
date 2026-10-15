from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_existing_as_approved(apps, schema_editor):
    """Existing orders already went through the old flow; never block them."""
    SAPSalesorder = apps.get_model('so', 'SAPSalesorder')
    SAPSalesorder.objects.all().update(finance_status='Approved')


class Migration(migrations.Migration):

    dependencies = [
        ('so', '0118_polinebaseline'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='sapsalesorder',
            name='finance_status',
            field=models.CharField(
                choices=[('Pending', 'Pending'), ('Approved', 'Approved'), ('Rejected', 'Rejected')],
                default='Pending',
                help_text='Finance approval (required before manager/MD for non-cash customers when enabled)',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='sapsalesorder',
            name='finance_updated_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='sapsalesorder_finance_approvals', to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='sapsalesorder',
            name='finance_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='sapsalesorder',
            name='finance_remarks',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_existing_as_approved, migrations.RunPython.noop),
    ]
