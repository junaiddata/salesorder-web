from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bangladeshpi', '0005_item_foc'),
    ]

    operations = [
        migrations.AddField(
            model_name='bangladeshpi',
            name='bank_freight_charges',
            field=models.DecimalField(decimal_places=2, default=Decimal('0'), max_digits=14),
        ),
    ]
