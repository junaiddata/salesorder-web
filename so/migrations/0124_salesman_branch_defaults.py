from django.db import migrations

# Salesman name -> branch (Junaid quotation PDF address). Only salesmen whose branch is still empty are touched.
BRANCH_BY_SALESMAN = {
    'R.STORES': 'RAK1',
    'R.MR.PARVEZ': 'RAK1',
    'R.QUSAIS': 'RAK2',
    'E.DEIRA 1': 'DEIRA',
    'R.DEIRA 2': 'DEIRA',
    'KADER': 'DEIRA',
    'R.AJMAN': 'AJMAN',
    'R.ABUDHABI': 'ABUDHABI',
}


def set_branches(apps, schema_editor):
    Salesman = apps.get_model('so', 'Salesman')
    for name, branch in BRANCH_BY_SALESMAN.items():
        Salesman.objects.filter(salesman_name=name, branch__isnull=True).update(branch=branch)
        Salesman.objects.filter(salesman_name=name, branch='').update(branch=branch)


class Migration(migrations.Migration):

    dependencies = [
        ('so', '0123_salesman_branch'),
    ]

    operations = [
        migrations.RunPython(set_branches, migrations.RunPython.noop),
    ]
