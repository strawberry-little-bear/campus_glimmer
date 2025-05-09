# listings/forms.py
from django import forms
from .models import Item, ItemImage, Category

class ItemForm(forms.ModelForm):
    class Meta:
        model = Item
        fields = ['title', 'description', 'price', 'category', 'condition']
        
class ItemImageForm(forms.ModelForm):
    image = forms.ImageField(label='图片')
    
    class Meta:
        model = ItemImage
        fields = ['image']
        
ItemImageFormSet = forms.inlineformset_factory(
    Item, ItemImage, form=ItemImageForm, extra=3, max_num=5
)