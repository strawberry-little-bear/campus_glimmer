from django import forms
from .models import Item, ItemImage, Category


class StyledModelFormMixin:
    def _style_fields(self):
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')


class ItemForm(StyledModelFormMixin, forms.ModelForm):
    class Meta:
        model = Item
        fields = ['title', 'description', 'price', 'category', 'condition']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 6, 'placeholder': '介绍商品的新旧程度、配件、交易方式等'}),
            'price': forms.NumberInput(attrs={'min': '0', 'step': '0.01', 'placeholder': '0.00'}),
            'condition': forms.TextInput(attrs={'placeholder': '例如：全新、9成新、轻微使用痕迹'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()
        self.fields['category'].widget.attrs['class'] = 'form-select'


class ItemImageForm(StyledModelFormMixin, forms.ModelForm):
    image = forms.ImageField(label='图片', required=False)

    class Meta:
        model = ItemImage
        fields = ['image']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style_fields()


ItemImageFormSet = forms.inlineformset_factory(
    Item, ItemImage, form=ItemImageForm, extra=3, max_num=5, can_delete=True
)
