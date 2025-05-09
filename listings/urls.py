# listings/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('', views.item_list, name='item_list'),
    path('category/<int:category_id>/', views.item_list, name='item_list_by_category'),
    path('item/<int:item_id>/', views.item_detail, name='item_detail'),
    path('item/new/', views.new_item, name='new_item'),
    path('item/<int:item_id>/edit/', views.edit_item, name='edit_item'),
    path('item/<int:item_id>/delete/', views.delete_item, name='delete_item'),
    path('item/<int:item_id>/mark_sold/', views.mark_sold, name='mark_sold'),
    path('my_items/', views.my_items, name='my_items'),
    path('search/', views.search_items, name='search_items'),
]