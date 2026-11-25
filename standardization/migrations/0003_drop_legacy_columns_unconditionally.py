"""
Migration 0003: Unconditionally drop the legacy `target_normality` and `factor`
columns using native Postgres DDL (DROP COLUMN IF EXISTS).

Background:
  Some deployed databases still carry a `target_normality NOT NULL` column
  (and sometimes a `factor` column) from a schema predating migration 0001.
  Migration 0002 attempted to drop them via Django introspection, but that
  approach silently skips columns that the introspection cache considers absent
  even when the physical column is still present.  Postgres's own
  `IF EXISTS` clause is the reliable alternative.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("standardization", "0002_retire_target_normality_input"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE standardization_standardization
                    DROP COLUMN IF EXISTS target_normality;

                ALTER TABLE standardization_standardization
                    DROP COLUMN IF EXISTS factor;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]

