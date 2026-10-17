from django.db import migrations


def sync_sequences(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'postgresql':
        return

    from django.core.management.color import no_style
    models = apps.get_models()
    sql_list = connection.ops.sequence_reset_sql(no_style(), list(models))
    with connection.cursor() as cursor:
        for sql in sql_list:
            cursor.execute(sql)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('job_tickets', '0115_alter_companyprofile_company_name_and_more'),
    ]

    operations = [
        migrations.RunPython(sync_sequences, noop),
    ]
