import datetime

from django.db import migrations, models


def _parse(text):
    """A saved Stock Availability text that is already a date (2026-11-05, 05.11.2026, 05/11/2026), else None."""
    text = (text or '').strip()
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def keep_dates(apps, schema_editor):
    Quotation = apps.get_model('quotation', 'Quotation')
    for q in Quotation.objects.exclude(stock_availability=''):
        d = _parse(q.stock_availability)
        if d:
            Quotation.objects.filter(pk=q.pk).update(stock_availability_date=d)


class Migration(migrations.Migration):

    dependencies = [
        ('quotation', '0003_quotationitem_item_no'),
    ]

    operations = [
        migrations.AddField(
            model_name='quotation',
            name='project_reference',
            field=models.CharField(blank=True, max_length=300),
        ),
        # Stock Availability: free text -> date. Existing values that are already dates are kept; any other
        # free text (e.g. "Ready stock") has no date to convert to and is cleared.
        migrations.AddField(
            model_name='quotation',
            name='stock_availability_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.RunPython(keep_dates, migrations.RunPython.noop),
        migrations.RemoveField(model_name='quotation', name='stock_availability'),
        migrations.RenameField(model_name='quotation', old_name='stock_availability_date', new_name='stock_availability'),
    ]
