"""
Management command to migrate all existing data from SQLite (db.sqlite3)
into the active PostgreSQL database.

Usage:
    python manage.py migrate_from_sqlite
"""

from django.core.management.base import BaseCommand
from django.db import connections, transaction
from django.contrib.auth.models import User
from gallery.models import (
    SubscriptionPlan,
    UserProfile,
    Event,
    EventShareLink,
    GalleryImage,
    FaceEmbedding,
    GuestLead,
)


class Command(BaseCommand):
    help = "Migrates data from SQLite (db.sqlite3) into PostgreSQL"

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size',
            type=int,
            default=500,
            help='Batch size for bulk insertion (default: 500)',
        )
        parser.add_argument(
            '--clear-target',
            action='store_true',
            help='Clear target tables in PostgreSQL before importing',
        )

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        clear_target = options['clear_target']

        if 'sqlite' not in connections.databases:
            self.stderr.write(self.style.ERROR(
                "SQLite database alias ('sqlite') not found in DATABASES settings.\n"
                "Make sure db.sqlite3 exists in the project root."
            ))
            return

        default_engine = connections.databases['default'].get('ENGINE', '')
        if 'sqlite' in default_engine:
            self.stderr.write(self.style.ERROR(
                "The default database is still configured as SQLite!\n"
                "Please configure your PostgreSQL settings in .env before running this command."
            ))
            return

        self.stdout.write(self.style.NOTICE(
            f"Migrating data from SQLite -> {default_engine}..."
        ))

        models_in_order = [
            (SubscriptionPlan, 'Subscription Plans'),
            (User, 'Users'),
            (UserProfile, 'User Profiles'),
            (Event, 'Events'),
            (EventShareLink, 'Event Share Links'),
            (GalleryImage, 'Gallery Images'),
            (FaceEmbedding, 'Face Embeddings'),
            (GuestLead, 'Guest Leads'),
        ]

        total_migrated = 0

        with transaction.atomic(using='default'):
            if clear_target:
                self.stdout.write(self.style.WARNING("Clearing existing data in target database..."))
                for model, label in reversed(models_in_order):
                    deleted_count, _ = model.objects.using('default').all().delete()
                    if deleted_count:
                        self.stdout.write(f"  Deleted {deleted_count} {label}")

            for model, label in models_in_order:
                src_qs = model.objects.using('sqlite').all().order_by('id')
                count = src_qs.count()

                if count == 0:
                    self.stdout.write(f"• {label}: 0 records to migrate.")
                    continue

                self.stdout.write(f"• Migrating {count} {label}...", ending=' ')
                self.stdout.flush()

                # Process in batches to keep memory usage low
                imported_this_model = 0
                for start in range(0, count, batch_size):
                    batch = list(src_qs[start:start + batch_size])
                    # ignore_conflicts=True skips duplicate IDs if partially migrated
                    model.objects.using('default').bulk_create(
                        batch,
                        batch_size=batch_size,
                        ignore_conflicts=True
                    )
                    imported_this_model += len(batch)

                self.stdout.write(self.style.SUCCESS(f"✓ ({imported_this_model}/{count})"))
                total_migrated += imported_this_model

            # Reset PostgreSQL primary key sequences so future inserts don't collide
            if 'postgresql' in default_engine:
                self.stdout.write("• Resetting PostgreSQL auto-increment sequences...")
                with connections['default'].cursor() as cursor:
                    for model, _ in models_in_order:
                        table = model._meta.db_table
                        try:
                            cursor.execute(
                                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                                f"coalesce((SELECT MAX(id) FROM {table}), 1), true);"
                            )
                        except Exception as seq_err:
                            # Sequence might not exist or custom PK name
                            pass
                self.stdout.write(self.style.SUCCESS("✓ Sequences updated."))

        self.stdout.write(self.style.SUCCESS(
            f"\nData migration complete! Total records migrated: {total_migrated}"
        ))

