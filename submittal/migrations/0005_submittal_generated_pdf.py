# Generated manually

from django.db import migrations, models


def generated_pdf_path(instance, filename):
    return f'submittal/generated/submittal_{instance.pk}.pdf'


class Migration(migrations.Migration):

    dependencies = [
        ('submittal', '0004_submittal_index_items'),
    ]

    operations = [
        migrations.AddField(
            model_name='submittal',
            name='generated_pdf',
            field=models.FileField(blank=True, null=True, upload_to='submittal/generated/'),
        ),
        migrations.AddField(
            model_name='submittal',
            name='pdf_generated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
