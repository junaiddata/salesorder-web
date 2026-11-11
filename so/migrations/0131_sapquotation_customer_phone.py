from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('so', '0130_alabama_item_links'),
    ]

    operations = [
        migrations.AddField(
            model_name='sapquotation',
            name='customer_phone',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
