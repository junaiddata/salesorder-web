from django.db import migrations

# Starting brand list for the Alabama catalog (editable afterwards in the admin: Alabama feed brands).
EXACT = [
    'JOMIX-PVC', 'JOMIX-SANITARYWARE', 'JOMIX', 'JOMIX-PUMP', 'JOMIX-VALVE', 'JOFLEX', 'FERROLI', 'HOME CREATOR',
    'KLUDI', 'ARISTON-SOLAR', 'ARISTON - ITALY', 'ARISTON - CHINA', 'ARISTON - BELGIUM', 'GROHE', 'VERA-CHINA',
    'VERA SOLAR', 'VERA-INDIA', 'AQUAVERA', 'LAMBORGINI', 'FRESH',
]
PREFIX = ['RAKTHERM', 'GF']


def seed(apps, schema_editor):
    Brand = apps.get_model('alabama', 'AlabamaFeedBrand')
    for name in EXACT:
        Brand.objects.get_or_create(brand=name, match_type='exact')
    for name in PREFIX:
        Brand.objects.get_or_create(brand=name, match_type='prefix')


def unseed(apps, schema_editor):
    Brand = apps.get_model('alabama', 'AlabamaFeedBrand')
    Brand.objects.filter(brand__in=EXACT, match_type='exact').delete()
    Brand.objects.filter(brand__in=PREFIX, match_type='prefix').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('alabama', '0010_alabama_item_master'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
