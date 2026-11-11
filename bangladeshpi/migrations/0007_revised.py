from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bangladeshpi', '0006_bank_freight'),
    ]

    operations = [
        migrations.AddField(
            model_name='bangladeshpi',
            name='is_revised',
            field=models.BooleanField(default=False),
        ),
    ]
