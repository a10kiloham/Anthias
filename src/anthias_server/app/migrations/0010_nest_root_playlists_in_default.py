"""Re-home parentless playlists into the Default playlist.

The Schedule tab renders the Default playlist's items — assets and
nested playlists — as one reorderable list. Playlists created before
this change were roots beside Default: they played (the evaluator
walks every root) but had no slot in the schedule list, so they
couldn't be ordered relative to assets. Append each one as a child
item of Default, preserving their existing root order.
"""

from django.db import migrations


def _nest_roots(apps, schema_editor):
    Playlist = apps.get_model('anthias_app', 'Playlist')
    PlaylistItem = apps.get_model('anthias_app', 'PlaylistItem')

    default = Playlist.objects.filter(is_default=True).first()
    if default is None:
        return

    parented_ids = set(
        PlaylistItem.objects.filter(
            child_playlist__isnull=False
        ).values_list('child_playlist_id', flat=True)
    )
    roots = [
        p
        for p in Playlist.objects.exclude(pk=default.pk).order_by(
            'position', 'playlist_id'
        )
        if p.playlist_id not in parented_ids
    ]
    if not roots:
        return

    last = (
        PlaylistItem.objects.filter(playlist=default)
        .order_by('-position')
        .first()
    )
    next_position = last.position + 1 if last else 0
    PlaylistItem.objects.bulk_create(
        PlaylistItem(
            playlist=default,
            child_playlist=playlist,
            position=next_position + offset,
        )
        for offset, playlist in enumerate(roots)
    )


class Migration(migrations.Migration):
    dependencies = [
        ('anthias_app', '0009_backfill_default_playlist'),
    ]

    operations = [
        migrations.RunPython(_nest_roots, migrations.RunPython.noop),
    ]
