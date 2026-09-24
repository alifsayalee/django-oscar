import secrets

from django.db import migrations


def create_install_reference(apps, schema_editor):
    InstallReference = apps.get_model('paypal_payments', 'InstallReference')
    if not InstallReference.objects.exists():
        InstallReference.objects.create(prefix='osc%s' % secrets.token_hex(4))


class Migration(migrations.Migration):

    dependencies = [
        ('paypal_payments', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_install_reference, migrations.RunPython.noop),
    ]
