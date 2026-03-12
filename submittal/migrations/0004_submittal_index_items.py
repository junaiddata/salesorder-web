from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('submittal', '0003_alter_submittalmaterial_brand_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='submittal',
            name='index_format',
        ),
        migrations.RemoveField(
            model_name='submittal',
            name='index_client_pdf',
        ),
        migrations.AddField(
            model_name='submittal',
            name='index_items',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Ordered list of index entries: [{label, included}]"
            ),
        ),
    ]
