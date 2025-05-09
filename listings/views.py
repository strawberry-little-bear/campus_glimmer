# listings/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q
from .models import Item, Category
from .forms import ItemForm, ItemImageFormSet

def home(request):
    categories = Category.objects.all()
    recent_items = Item.objects.filter(status='available').order_by('-created_at')[:8]
    context = {
        'categories': categories,
        'recent_items': recent_items
    }
    return render(request, 'listings/home.html', context)

def item_list(request, category_id=None):
    categories = Category.objects.all()
    
    if category_id:
        category = get_object_or_404(Category, id=category_id)
        items = Item.objects.filter(category=category, status='available').order_by('-created_at')
        title = f'{category.name}类别下的商品'
    else:
        items = Item.objects.filter(status='available').order_by('-created_at')
        title = '所有可购买商品'
        
    context = {
        'categories': categories,
        'items': items,
        'title': title
    }
    return render(request, 'listings/item_list.html', context)

def item_detail(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    context = {'item': item}
    return render(request, 'listings/item_detail.html', context)

@login_required
def new_item(request):
    if request.method == 'POST':
        form = ItemForm(request.POST)
        formset = ItemImageFormSet(request.POST, request.FILES)
        
        if form.is_valid() and formset.is_valid():
            item = form.save(commit=False)
            item.seller = request.user
            item.save()
            
            for form in formset:
                if form.cleaned_data and form.cleaned_data.get('image'):
                    image = form.save(commit=False)
                    image.item = item
                    image.save()
                    
            messages.success(request, '商品已成功发布！')
            return redirect('item_detail', item_id=item.id)
    else:
        form = ItemForm()
        formset = ItemImageFormSet()
        
    context = {
        'form': form,
        'formset': formset,
        'title': '发布新商品'
    }
    return render(request, 'listings/item_form.html', context)

@login_required
def edit_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    
    # 检查当前用户是否是卖家
    if item.seller != request.user:
        messages.error(request, '您没有权限编辑此商品！')
        return redirect('item_detail', item_id=item.id)
        
    if request.method == 'POST':
        form = ItemForm(request.POST, instance=item)
        formset = ItemImageFormSet(request.POST, request.FILES, instance=item)
        
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.success(request, '商品信息已更新！')
            return redirect('item_detail', item_id=item.id)
    else:
        form = ItemForm(instance=item)
        formset = ItemImageFormSet(instance=item)
        
    context = {
        'form': form,
        'formset': formset,
        'title': '编辑商品',
        'item': item
    }
    return render(request, 'listings/item_form.html', context)

@login_required
def delete_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    
    # 检查当前用户是否是卖家
    if item.seller != request.user:
        messages.error(request, '您没有权限删除此商品！')
        return redirect('item_detail', item_id=item.id)
        
    if request.method == 'POST':
        item.delete()
        messages.success(request, '商品已成功删除！')
        return redirect('my_items')
        
    context = {'item': item}
    return render(request, 'listings/item_confirm_delete.html', context)

@login_required
def mark_sold(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    
    # 检查当前用户是否是卖家
    if item.seller != request.user:
        messages.error(request, '您没有权限更改此商品状态！')
        return redirect('item_detail', item_id=item.id)
        
    if request.method == 'POST':
        status = request.POST.get('status')
        if status in [s[0] for s in Item.STATUS_CHOICES]:
            item.status = status
            item.save()
            messages.success(request, f'商品状态已更新为{dict(Item.STATUS_CHOICES)[status]}！')
        return redirect('item_detail', item_id=item.id)
        
    context = {'item': item}
    return render(request, 'listings/mark_sold.html', context)

@login_required
def my_items(request):
    items = Item.objects.filter(seller=request.user).order_by('-created_at')
    context = {
        'items': items,
        'title': '我的商品'
    }
    return render(request, 'listings/my_items.html', context)

def search_items(request):
    query = request.GET.get('q', '')
    
    if query:
        items = Item.objects.filter(
            Q(title__icontains=query) | 
            Q(description__icontains=query),
            status='available'
        ).order_by('-created_at')
    else:
        items = Item.objects.none()
        
    context = {
        'items': items,
        'query': query,
        'title': f'搜索结果: {query}'
    }
    return render(request, 'listings/search_results.html', context)