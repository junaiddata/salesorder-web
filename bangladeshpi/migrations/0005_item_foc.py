from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bangladeshpi', '0004_private_seals'),
    ]

    operations = [
        migrations.AddField(
            model_name='bangladeshpiitem',
            name='is_foc',
            field=models.BooleanField(default=False),
        ),
    ]
