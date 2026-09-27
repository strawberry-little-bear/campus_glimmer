from django import forms

from .models import Comment, PrivateMessage


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ['content']
        widgets = {'content': forms.Textarea(attrs={'rows': 3, 'class': 'form-control'})}


class PrivateMessageForm(forms.ModelForm):
    class Meta:
        model = PrivateMessage
        fields = ['content']
        widgets = {'content': forms.Textarea(attrs={'rows': 4, 'class': 'form-control', 'placeholder': '写下你想说的话…'})}
