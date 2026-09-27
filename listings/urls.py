from django.urls import path
from . import views

urlpatterns = [
    path('', views.item_list, name='item_list'),
    path('category/<int:category_id>/', views.item_list, name='item_list_by_category'),
    path('item/<int:item_id>/', views.item_detail, name='item_detail'),
    path('item/<int:item_id>/report/', views.report_item, name='report_item'),
    path('item/<int:item_id>/order/', views.create_order, name='create_order'),
    path('item/new/', views.new_item, name='new_item'),
    path('item/<int:item_id>/edit/', views.edit_item, name='edit_item'),
    path('item/<int:item_id>/delete/', views.delete_item, name='delete_item'),
    path('item/<int:item_id>/mark_sold/', views.mark_sold, name='mark_sold'),
    path('item/<int:item_id>/favorite/', views.toggle_favorite, name='toggle_favorite'),
    path('favorites/', views.favorite_list, name='favorite_list'),
    path('orders/', views.my_orders, name='my_orders'),
    path('history/', views.browsing_history, name='browsing_history'),
    path('order/<int:order_id>/', views.order_detail, name='order_detail'),
    path('order/<int:order_id>/status/', views.update_order_status, name='update_order_status'),
    path('my_items/', views.my_items, name='my_items'),
    path('search/', views.search_items, name='search_items'),
]
