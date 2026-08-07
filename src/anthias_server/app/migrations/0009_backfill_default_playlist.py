"""Backfill the Default playlist.

Pre-playlist Anthias had exactly one implicit playlist: every asset,
ordered by ``Asset.play_order``. Make that explicit — a ``Playlist``
row with ``is_default=True`` holding one ``PlaylistItem`` per existing
asset at its current order — so the playlist-aware evaluators see a
byte-identical screen before and after the upgrade, and no asset is
left outside a playlist (a stranded asset would silently stop
playing).

Every asset (enabled or not) gets an item: playlist membership is
orthogonal to ``is_enabled``, mirroring how the home page's
Active/Inactive split never removed rows from the implicit playlist.
"""

from django.db import migrations


def _create_default_playlist(apps, schema_editor):
    Asset = apps.get_model('anthias_app', 'Asset')
    Playlist = apps.get_model('anthias_app', 'Playlist')
    PlaylistItem = apps.get_model('anthias_app', 'PlaylistItem')

    if Playlist.objects.filter(is_default=True).exists():
        return

    default = Playlist.objects.create(
        name='Default',
        is_default=True,
        is_enabled=True,
        repeat=True,
        position=0,
    )
    items = [
        PlaylistItem(playlist=default, asset=asset, position=index)
        for index, asset in enumerate(
            Asset.objects.order_by('play_order', 'asset_id')
        )
    ]
    PlaylistItem.objects.bulk_create(items)


def _drop_default_playlist(apps, schema_editor):
    Playlist = apps.get_model('anthias_app', 'Playlist')
    Playlist.objects.filter(is_default=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('anthias_app', '0008_playlist_playlistitem'),
    ]

    operations = [
        migrations.RunPython(
            _create_default_playlist, _drop_default_playlist
        ),
    ]
