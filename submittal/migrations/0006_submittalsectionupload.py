from django.db import migrations, models
import django.db.models.deletion


def section_upload_path(instance, filename):
    return f'submittal/section_uploads/{instance.submittal_id or "new"}/{filename}'


class Migration(migrations.Migration):

    dependencies = [
        ('submittal', '0005_submittal_generated_pdf'),
    ]

    operations = [
        migrations.CreateModel(
            name='SubmittalSectionUpload',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('index_label', models.CharField(help_text='Must match the index item label exactly', max_length=255)),
                ('file', models.FileField(upload_to=section_upload_path)),
                ('uploaded_at', models.DateTimeField(auto_now=True)),
                ('submittal', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='section_uploads',
                    to='submittal.submittal',
                )),
            ],
            options={
                'ordering': ['submittal', 'index_label'],
                'unique_together': {('submittal', 'index_label')},
            },
        ),
    ]
