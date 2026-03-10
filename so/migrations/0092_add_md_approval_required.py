from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('so', '0091_add_approval_status_choices'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sapsalesorder',
            name='approval_status',
            field=models.CharField(
                choices=[
                    ('Pending', 'Pending'),
                    ('Approved', 'Approved'),
                    ('Rejected', 'Rejected'),
                    ('DO Completed', 'DO Completed'),
                    ('Partial DO', 'Partial DO'),
                    ('Trade License Expired', 'Trade License Expired'),
                    ('MD Approval Required', 'MD Approval Required'),
                ],
                default='Pending',
                help_text='Approval status (Admin only)',
                max_length=30,
            ),
        ),
    ]
