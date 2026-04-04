from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('so', '0104_unique_so_line'),
    ]

    operations = [
        migrations.AddField(
            model_name='sapsalesorder',
            name='management_remarks',
            field=models.TextField(
                blank=True,
                null=True,
                help_text='PDF remarks: shown on exported SO PDF; visible to all on order detail.',
            ),
        ),
    ]
