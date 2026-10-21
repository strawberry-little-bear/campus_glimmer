# accounts/urls.py
from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # /accounts/ 根路径落到个人主页，与 /messages/ 落到收件箱保持一致，避免只输命名空间根路径时吃 404
    path('', views.profile, name='accounts_home'),
    path('register/', views.register, name='register'),
    path('login/', auth_views.LoginView.as_view(template_name='accounts/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(template_name='accounts/logout.html', http_method_names=['get', 'post']), name='logout'),
    path('profile/', views.profile, name='profile'),
    path('user/<int:user_id>/', views.public_profile, name='public_profile'),
    path('profile/edit/', views.edit_profile, name='edit_profile'),
    path('profile/campus-verification/', views.campus_verification, name='campus_verification'),
    path('campus-verification/<uidb64>/<str:token>/', views.verify_campus_email, name='verify_campus_email'),
]