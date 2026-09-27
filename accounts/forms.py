from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import Profile


class StyledFormMixin:
    def _style_fields(self):
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')


class UserRegisterForm(StyledFormMixin, UserCreationForm):
    email = forms.EmailField(label='邮箱')

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']
        labels = {'username': '用户名', 'password1': '密码', 'password2': '确认密码'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class UserUpdateForm(StyledFormMixin, forms.ModelForm):
    email = forms.EmailField(label='邮箱')

    class Meta:
        model = User
        fields = ['username', 'email']
        labels = {'username': '用户名', 'email': '邮箱'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class ProfileUpdateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Profile
        fields = ['avatar', 'bio', 'student_id', 'wechat', 'phone']
        labels = {'avatar': '头像', 'bio': '个人简介', 'student_id': '学号', 'wechat': '微信', 'phone': '电话'}
        widgets = {'bio': forms.Textarea(attrs={'rows': 4, 'placeholder': '介绍一下自己吧～'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
