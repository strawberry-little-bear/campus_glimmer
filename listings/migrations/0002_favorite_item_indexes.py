from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('listings', '0001_initial'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='category',
            options={'ordering': ['name'], 'verbose_name': '商品分类', 'verbose_name_plural': '商品分类'},
        ),
        migrations.AlterModelOptions(
            name='item',
            options={'ordering': ['-created_at']},
        ),
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['status', '-created_at'], name='listings_it_status_7dd8d1_idx'),
        ),
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['category', 'status'], name='listings_it_categor_5ff2b2_idx'),
        ),
        migrations.CreateModel(
            name='Favorite',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='收藏时间')),
                ('item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='favorites', to='listings.item')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='favorites', to='auth.user')),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.AddConstraint(
            model_name='favorite',
            constraint=models.UniqueConstraint(fields=('user', 'item'), name='unique_user_item_favorite'),
        ),
    ]
