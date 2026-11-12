from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('so', '0133_sapquotationfollowup'),
    ]

    operations = [
        migrations.AddField(
            model_name='sapquotation',
            name='closing_remarks',
            field=models.TextField(blank=True, help_text='ClosingRemarks from SAP API (quotation terms: brand, stock status, validity). Read-only, overwritten by sync.', null=True),
        ),
    ]
