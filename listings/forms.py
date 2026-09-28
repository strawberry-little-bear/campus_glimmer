from datetime import timedelta

from django import forms
from django.db.models import Q
from django.utils import timezone

from .models import CampusLocation, Category, DemandPost, Item, ItemImage, MeetingAppointment, NotificationPreference, Order, OrderDispute, Rating, Report, SavedSearch


class StyledModelFormMixin:
    def _style_fields(self):
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')


class NotificationPreferenceForm(forms.ModelForm):
    class Meta:
        model = NotificationPreference
        fields = [
            'order_created', 'order_status', 'rating_received', 'message_received',
            'comment_received', 'saved_search_match', 'item_available', 'order_dispute',
            'order_expiring', 'order_expired', 'report_update', 'moderation_update',
            'operations_digest', 'demand_match',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget = forms.CheckboxInput(attrs={'class': 'form-check-input'})


class ItemForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = Item
        fields = ['title', 'description', 'price', 'category', 'location', 'condition']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 6, 'placeholder': '介绍商品的新旧程度、配件、交易方式等'}),
            'price': forms.NumberInput(attrs={'min': '0', 'step': '0.01', 'placeholder': '0.00'}),
            'condition': forms.TextInput(attrs={'placeholder': '例如：全新、9成新、轻微使用痕迹'}),
            'location': forms.Select(attrs={'class': 'form-select'}),
        }
        help_texts = {
            'location': '填写方便见面的地点，让交易更清晰。',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['category'].widget.attrs['class'] = 'form-select'
        self.fields['location'].widget.attrs['class'] = 'form-select'
        self.fields['location'].queryset = CampusLocation.objects.filter(is_active=True)
        self.fields['location'].empty_label = '请选择交易地点（可选）'


class ItemImageForm(StyledModelFormMixin, forms.ModelForm):
    image = forms.ImageField(label='图片', required=False)

    class Meta:
        model = ItemImage
        fields = ['image']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


class ReportForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = Report
        fields = ['reason', 'detail']
        widgets = {
            'reason': forms.Select(attrs={'class': 'form-select'}),
            'detail': forms.Textarea(attrs={'rows': 5, 'placeholder': '请描述你发现的问题，帮助我们更快完成审核。'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['reason'].widget.attrs['class'] = 'form-select'


ItemImageFormSet = forms.inlineformset_factory(
    Item, ItemImage, form=ItemImageForm, extra=3, max_num=5, can_delete=True
)


class ReportReviewForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = Report
        fields = ['status', 'review_note']
        widgets = {
            'status': forms.Select(attrs={'class': 'form-select'}),
            'review_note': forms.Textarea(attrs={
                'rows': 5,
                'placeholder': '记录核查依据、处理结果以及后续建议。',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['status'].widget.attrs['class'] = 'form-select'


class OrderForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = Order
        fields = ['meeting_location', 'buyer_note']
        widgets = {
            'meeting_location': forms.Select(attrs={'class': 'form-select'}),
            'buyer_note': forms.Textarea(attrs={'rows': 4, 'placeholder': '例如：周三晚课后在图书馆东门见面。'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['meeting_location'].widget.attrs['class'] = 'form-select'
        self.fields['meeting_location'].queryset = CampusLocation.objects.filter(is_active=True)
        self.fields['meeting_location'].empty_label = '沿用商品交易地点'


class DeliveryCodeForm(forms.Form):
    code = forms.CharField(
        label='交付确认码',
        min_length=6,
        max_length=6,
        strip=True,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'inputmode': 'numeric',
            'autocomplete': 'one-time-code',
            'placeholder': '输入买家提供的 6 位数字',
        }),
        help_text='确认码只用于核对当面交付，不要在公开留言中发送。',
    )

    def clean_code(self):
        code = self.cleaned_data['code']
        if not code.isdigit():
            raise forms.ValidationError('交付确认码必须是 6 位数字。')
        return code


class MeetingAppointmentForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = MeetingAppointment
        fields = ['start_at', 'end_at', 'location']
        widgets = {
            'start_at': forms.DateTimeInput(
                format='%Y-%m-%dT%H:%M',
                attrs={'class': 'form-control', 'type': 'datetime-local'},
            ),
            'end_at': forms.DateTimeInput(
                format='%Y-%m-%dT%H:%M',
                attrs={'class': 'form-control', 'type': 'datetime-local'},
            ),
            'location': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['start_at'].input_formats = ['%Y-%m-%dT%H:%M']
        self.fields['end_at'].input_formats = ['%Y-%m-%dT%H:%M']
        location_queryset = CampusLocation.objects.filter(is_active=True)
        if self.instance and self.instance.location_id:
            location_queryset = CampusLocation.objects.filter(
                Q(is_active=True) | Q(pk=self.instance.location_id),
            )
        self.fields['location'].queryset = location_queryset
        self.fields['location'].required = False
        self.fields['start_at'].help_text = '建议预留至少 15 分钟，时间以东八区显示。'
        self.fields['end_at'].help_text = '单次交付安排最长 12 小时。'

    def clean(self):
        cleaned_data = super().clean()
        start_at = cleaned_data.get('start_at')
        end_at = cleaned_data.get('end_at')
        if not start_at or not end_at:
            return cleaned_data
        if timezone.is_naive(start_at):
            start_at = timezone.make_aware(start_at)
            cleaned_data['start_at'] = start_at
        if timezone.is_naive(end_at):
            end_at = timezone.make_aware(end_at)
            cleaned_data['end_at'] = end_at
        if end_at <= start_at:
            self.add_error('end_at', '结束时间必须晚于开始时间。')
            return cleaned_data
        now = timezone.now()
        if start_at < now + timedelta(minutes=15):
            self.add_error('start_at', '开始时间至少应晚于当前时间 15 分钟。')
        if start_at > now + timedelta(days=60):
            self.add_error('start_at', '最多只能安排未来 60 天内的时间。')
        if end_at - start_at > timedelta(hours=12):
            self.add_error('end_at', '单次交付安排不能超过 12 小时。')
        return cleaned_data


class RatingForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = Rating
        fields = ['score', 'comment']
        widgets = {
            'score': forms.Select(attrs={'class': 'form-select'}),
            'comment': forms.Textarea(attrs={'rows': 4, 'placeholder': '分享这次交易的体验，帮助其他同学做出判断。'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['score'].widget.attrs['class'] = 'form-select'


class SavedSearchForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = SavedSearch
        fields = ['name', 'query', 'condition', 'category', 'location', 'min_price', 'max_price']
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': '例如：图书馆附近的考研资料'}),
            'query': forms.HiddenInput(),
            'condition': forms.HiddenInput(),
            'category': forms.HiddenInput(),
            'location': forms.HiddenInput(),
            'min_price': forms.HiddenInput(),
            'max_price': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['category'].queryset = Category.objects.all()
        self.fields['location'].queryset = CampusLocation.objects.filter(is_active=True)

    def clean(self):
        cleaned_data = super().clean()
        has_criteria = any((
            bool(cleaned_data.get('query')),
            bool(cleaned_data.get('condition')),
            cleaned_data.get('category') is not None,
            cleaned_data.get('location') is not None,
            cleaned_data.get('min_price') is not None,
            cleaned_data.get('max_price') is not None,
        ))
        if not has_criteria:
            raise forms.ValidationError('至少保留一个搜索条件，才能创建关注提醒。')
        min_price = cleaned_data.get('min_price')
        max_price = cleaned_data.get('max_price')
        if min_price is not None and max_price is not None and min_price > max_price:
            self.add_error('max_price', '最高价格不能低于最低价格。')
        return cleaned_data


class DisputeForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = OrderDispute
        fields = ['reason', 'detail']
        widgets = {
            'reason': forms.Select(attrs={'class': 'form-select'}),
            'detail': forms.Textarea(attrs={
                'rows': 6,
                'placeholder': '请描述发生了什么、你希望平台如何协助处理。',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['reason'].widget.attrs['class'] = 'form-select'


class DisputeResolutionForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = OrderDispute
        fields = ['status', 'resolution_note']
        widgets = {
            'status': forms.Select(attrs={'class': 'form-select'}),
            'resolution_note': forms.Textarea(attrs={
                'rows': 5,
                'placeholder': '记录核实结果、处理依据和后续建议。',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['status'].choices = [
            choice for choice in self.fields['status'].choices
            if choice[0] in {'resolved', 'rejected'}
        ]
        self.fields['status'].widget.attrs['class'] = 'form-select'


class DemandPostForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = DemandPost
        fields = ['title', 'description', 'category', 'location', 'min_price', 'max_price', 'expires_at']
        widgets = {
            'title': forms.TextInput(attrs={'placeholder': '例如：求一台适合宿舍使用的显示器'}),
            'description': forms.Textarea(attrs={'rows': 6, 'placeholder': '描述品牌、规格、新旧程度、预算和交易要求。'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'location': forms.Select(attrs={'class': 'form-select'}),
            'min_price': forms.NumberInput(attrs={'min': '0', 'step': '0.01', 'placeholder': '可选'}),
            'max_price': forms.NumberInput(attrs={'min': '0', 'step': '0.01', 'placeholder': '可选'}),
            'expires_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
        }
        help_texts = {
            'location': '填写方便见面的区域，卖家可以据此判断是否方便交付。',
            'expires_at': '超过截止时间后，求购信息会自动从公开列表中隐藏。',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['category'].queryset = Category.objects.all()
        self.fields['category'].empty_label = '请选择分类（可选）'
        self.fields['location'].queryset = CampusLocation.objects.filter(is_active=True)
        self.fields['location'].empty_label = '请选择期望地点（可选）'
        if not self.instance.pk and not self.initial.get('expires_at'):
            from django.utils import timezone
            self.initial['expires_at'] = (timezone.now() + timezone.timedelta(days=30)).replace(second=0, microsecond=0)

    def clean(self):
        cleaned_data = super().clean()
        min_price = cleaned_data.get('min_price')
        max_price = cleaned_data.get('max_price')
        if min_price is not None and max_price is not None and min_price > max_price:
            self.add_error('max_price', '最高预算不能低于最低预算。')
        expires_at = cleaned_data.get('expires_at')
        if expires_at is not None:
            from django.utils import timezone
            if timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(expires_at)
                cleaned_data['expires_at'] = expires_at
            if expires_at <= timezone.now():
                self.add_error('expires_at', '截止时间必须晚于当前时间。')
            elif expires_at > timezone.now() + timezone.timedelta(days=90):
                self.add_error('expires_at', '截止时间不能超过 90 天。')
        return cleaned_data
