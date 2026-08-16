import django.db.models.deletion
from django.db import migrations, models

import anthias_server.app.models


class Migration(migrations.Migration):

    dependencies = [
        ('anthias_app', '0006_asset_metadata'),
    ]

    operations = [
        migrations.CreateModel(
            name='Playlist',
            fields=[
                (
                    'playlist_id',
                    models.TextField(
                        default=anthias_server.app.models.generate_asset_id,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ('name', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'playlists',
                'ordering': ['name'],
            },
        ),
        migrations.AddField(
            model_name='asset',
            name='playlist',
            field=models.ForeignKey(
                blank=True,
                db_column='playlist_id',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='assets',
                to='anthias_app.playlist',
            ),
        ),
    ]
