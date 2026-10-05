"""Database-level enforcement of append-only tables (PostgreSQL).

Even someone with application database credentials (or an admin using
pgAdmin) cannot UPDATE or DELETE audit events, ballots, evidence or payment
history without first dropping these triggers - which itself requires DDL
rights and leaves a trace in the database logs.
"""
from django.db import migrations

APPEND_ONLY_TABLES = ['core_auditevent', 'elections_ballot', 'elections_evidenceitem', 'payments_paymentevent']


def create_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    # params=None: run the SQL verbatim - the '%' placeholders belong to
    # PL/pgSQL's RAISE, not to client-side parameter interpolation.
    schema_editor.execute("""
        CREATE OR REPLACE FUNCTION flexyvotes_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Table % is append-only (% blocked)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$ LANGUAGE plpgsql;
    """, params=None)
    for table in APPEND_ONLY_TABLES:
        schema_editor.execute(f'DROP TRIGGER IF EXISTS {table}_append_only ON {table};')
        schema_editor.execute(
            f'CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} '
            f'FOR EACH ROW EXECUTE FUNCTION flexyvotes_append_only();'
        )


def drop_triggers(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    for table in APPEND_ONLY_TABLES:
        schema_editor.execute(f'DROP TRIGGER IF EXISTS {table}_append_only ON {table};')
    schema_editor.execute('DROP FUNCTION IF EXISTS flexyvotes_append_only();')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
        ('elections', '0001_initial'),
        ('payments', '0001_initial'),
        ('voting', '0037_migrate_legacy_data'),
    ]

    operations = [
        migrations.RunPython(create_triggers, drop_triggers),
    ]
