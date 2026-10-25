from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Category, Favorite, FavoriteCollection, Item


class FavoritePaginationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='favorite-owner', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='favorite-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='心愿单分页分类')

    def make_favorite(self, title, *, user=None, collection=None):
        item = Item.objects.create(
            title=title,
            description='心愿单分页测试商品',
            price='26.00',
            category=self.category,
            condition='9成新',
            seller=self.other,
        )
        return Favorite.objects.create(
            user=user or self.user,
            item=item,
            collection=collection,
        )

    def test_all_favorites_are_paginated_and_out_of_range_falls_back(self):
        for index in range(13):
            self.make_favorite(f'心愿单分页商品 {index:02d}')
        self.make_favorite('别人的收藏', user=self.other)
        self.client.force_login(self.user)

        first_page = self.client.get(reverse('favorite_list'))
        last_page = self.client.get(reverse('favorite_list'), {'page': 99})

        self.assertEqual(first_page.context['favorite_total_count'], 13)
        self.assertEqual(first_page.context['page_obj'].paginator.count, 13)
        self.assertEqual(len(first_page.context['favorites']), 12)
        self.assertEqual(last_page.context['page_obj'].number, 2)
        self.assertEqual(len(last_page.context['favorites']), 1)
        self.assertContains(first_page, '第 1–12 件 · 共 13 件')
        self.assertContains(last_page, '第 13–13 件 · 共 13 件')
        self.assertNotContains(first_page, '别人的收藏')

    def test_collection_filter_and_total_count_are_kept_across_pages(self):
        collection = FavoriteCollection.objects.create(
            user=self.user, name='教材与复习资料',
        )
        for index in range(13):
            self.make_favorite(
                f'分组收藏 {index:02d}', collection=collection,
            )
        self.make_favorite('未分组收藏一')
        self.make_favorite('未分组收藏二')
        self.client.force_login(self.user)

        first_page = self.client.get(
            reverse('favorite_list'), {'collection': collection.id},
        )
        second_page = self.client.get(
            reverse('favorite_list'), {'collection': collection.id, 'page': 2},
        )

        self.assertEqual(first_page.context['favorite_total_count'], 15)
        self.assertEqual(first_page.context['page_obj'].paginator.count, 13)
        self.assertEqual(len(second_page.context['favorites']), 1)
        self.assertContains(first_page, '第 1–12 件 · 共 13 件')
        self.assertContains(
            second_page,
            f'?collection={collection.id}&amp;page=1',
            html=False,
        )
        self.assertNotContains(second_page, '未分组收藏一')
        self.assertNotContains(second_page, '未分组收藏二')
