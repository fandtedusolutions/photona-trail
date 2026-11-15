import sys
from django.core.management.base import BaseCommand
from django.core.cache import cache
from gallery.models import Event, GalleryImage
from gallery.tasks import process_event_fair_chunk_task, trigger_event_fair_indexing


class Command(BaseCommand):
    help = 'Resume AI face indexing for events with pending unindexed photos.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--event',
            type=str,
            help='Event ID or slug to index. If omitted, checks all events with pending photos.'
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run indexing synchronously in the foreground instead of queuing to Celery.'
        )
        parser.add_argument(
            '--chunk-size',
            type=int,
            default=30,
            help='Chunk size for batch processing (default: 30).'
        )

    def handle(self, *args, **options):
        event_arg = options.get('event')
        run_sync = options.get('sync')
        chunk_size = options.get('chunk_size', 30)

        if event_arg:
            if event_arg.isdigit():
                events = list(Event.objects.filter(id=int(event_arg)))
            else:
                events = list(Event.objects.filter(slug=event_arg))
            if not events:
                self.stderr.write(self.style.ERROR(f"Event not found: {event_arg}"))
                return
        else:
            pending_event_ids = list(
                GalleryImage.objects.filter(total_faces=-1)
                .values_list('event_id', flat=True)
                .distinct()
            )
            events = list(Event.objects.filter(id__in=pending_event_ids))

        if not events:
            self.stdout.write(self.style.SUCCESS("All photos are fully indexed! No pending photos found."))
            return

        for event in events:
            pending_count = GalleryImage.objects.filter(event_id=event.id, total_faces=-1).count()
            total_count = GalleryImage.objects.filter(event_id=event.id).count()
            self.stdout.write(
                self.style.NOTICE(f"Event '{event.name}' (ID: {event.id}): {pending_count}/{total_count} photos pending face detection.")
            )

            # Clear any stale lock
            lock_key = f"ai_indexing_active_{event.id}"
            cache.delete(lock_key)

            if run_sync:
                self.stdout.write(f"Starting synchronous indexing (batch size {chunk_size})...")
                while True:
                    has_pending = GalleryImage.objects.filter(event_id=event.id, total_faces=-1).exists()
                    if not has_pending:
                        break
                    process_event_fair_chunk_task(event.id, chunk_size=chunk_size)
                    remaining = GalleryImage.objects.filter(event_id=event.id, total_faces=-1).count()
                    self.stdout.write(f"  Processed chunk. Remaining pending: {remaining}/{total_count}")
                self.stdout.write(self.style.SUCCESS(f"Finished indexing event '{event.name}'!"))
            else:
                trigger_event_fair_indexing(event.id, force=True)
                self.stdout.write(self.style.SUCCESS(f"Dispatched Celery indexing task for event '{event.name}'. Check Celery logs with 'sudo journalctl -u celery -f'."))
