from django import forms
from .models import CampusLocation, Category, Item, ItemImage, Report


class StyledModelFormMixin:
    def _style_fields(self):
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')


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
