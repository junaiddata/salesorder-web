from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bangladeshpi', '0001_initial'),
    ]

    operations = [
        # Terms are now bank-copy only; keep any text already saved.
        migrations.RenameField(
            model_name='bangladeshpi',
            old_name='terms_text',
            new_name='bank_terms_text',
        ),
        migrations.AddField(
            model_name='bangladeshpi',
            name='bank_payment',
            field=models.CharField(blank=True, max_length=400),
        ),
        migrations.AddField(
            model_name='bangladeshpi',
            name='bank_insurance',
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name='bangladeshpiitem',
            name='bank_unit_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
    ]
