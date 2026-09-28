from django import forms
from .models import CampusLocation, Category, Item, ItemImage, NotificationPreference, Order, OrderDispute, Rating, Report, SavedSearch


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
            'order_expiring', 'order_expired', 'report_update',
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
