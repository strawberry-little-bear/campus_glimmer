from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import CampusDomain, CampusVerification

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

class CampusVerificationForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = CampusVerification
        fields = ['campus_email']
        labels = {'campus_email': '校园邮箱'}
        widgets = {
            'campus_email': forms.EmailInput(attrs={
                'placeholder': '例如：name@university.edu.cn',
                'autocomplete': 'email',
            }),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['campus_email'].help_text = '验证链接会发送到这个邮箱。平台只保存邮箱、认证状态和不可逆的令牌哈希。'

    def clean_campus_email(self):
        email = self.cleaned_data['campus_email'].strip().lower()
        domain = email.rsplit('@', 1)[-1]
        domain_record = CampusDomain.objects.filter(domain=domain, is_active=True).first()
        if not domain_record:
            raise forms.ValidationError('暂不支持该邮箱域名，请联系管理员配置校园邮箱域名。')
        duplicate = CampusVerification.objects.filter(campus_email__iexact=email).exclude(user=self.user).first()
        if duplicate:
            raise forms.ValidationError('这个校园邮箱已经绑定了其他账号。')
        self.domain_record = domain_record
        return email
