# init_data.py
import os
import django

# 设置Django环境
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'campus_glimmer.settings')
django.setup()

from django.contrib.auth.models import User
from django.core.files import File
from listings.models import Category, Item, ItemImage
from django.utils import timezone
import random
import os

def create_categories():
    """创建商品类别"""
    categories = [
        {
            'name': '教材书籍',
            'description': '课本、教辅资料、考试复习资料等'
        },
        {
            'name': '电子产品',
            'description': '手机、电脑、耳机、相机等电子设备'
        },
        {
            'name': '生活用品',
            'description': '宿舍用品、日常生活必需品等'
        },
        {
            'name': '服装鞋帽',
            'description': '衣服、鞋子、帽子、包包等'
        },
        {
            'name': '体育用品',
            'description': '运动器材、健身装备等'
        },
        {
            'name': '票券礼品',
            'description': '电影票、演出票、礼品卡等'
        }
    ]
    
    for category_data in categories:
        Category.objects.get_or_create(
            name=category_data['name'],
            defaults={'description': category_data['description']}
        )
    
    print(f"已创建 {len(categories)} 个商品类别")

def create_test_users():
    """创建测试用户"""
    users = [
        {'username': 'test_user1', 'email': 'user1@example.com', 'password': 'testpassword123'},
        {'username': 'test_user2', 'email': 'user2@example.com', 'password': 'testpassword123'},
    ]
    
    created_users = []
    for user_data in users:
        user, created = User.objects.get_or_create(
            username=user_data['username'],
            defaults={
                'email': user_data['email'],
            }
        )
        
        if created:
            user.set_password(user_data['password'])
            user.save()
            
            # 设置用户资料
            profile = user.profile
            profile.bio = f"我是{user.username}，这是一个测试账户。"
            profile.student_id = f"20220{random.randint(1000, 9999)}"
            profile.wechat = f"{user.username}_wechat"
            profile.phone = f"1391234{random.randint(1000, 9999)}"
            profile.save()
            
        created_users.append(user)
    
    print(f"已创建 {len(created_users)} 个测试用户")
    return created_users

def create_test_items(users):
    """创建测试商品"""
    if not users:
        return
    
    categories = list(Category.objects.all())
    if not categories:
        print("没有找到商品类别，请先创建类别")
        return
    
    items_data = [
        {
            'title': '全新考研数学复习资料',
            'description': '全新2023版考研数学复习全书，只翻阅过几次，包含高数、线代、概率论三本，有少量笔记，保存完好。',
            'price': 50.00,
            'condition': '95成新',
            'status': 'available',
        },
        {
            'title': '小米平板5 Pro',
            'description': '去年购买的小米平板5 Pro，8+256GB，配件齐全，有轻微使用痕迹，性能完好，适合学习和娱乐。',
            'price': 1500.00,
            'condition': '9成新',
            'status': 'available',
        },
        {
            'title': '宿舍收纳神器',
            'description': '宿舍必备收纳盒套装，包含衣物收纳箱、书架、桌面整理架等，整套购买比单买便宜很多。',
            'price': 80.00,
            'condition': '全新',
            'status': 'available',
        },
        {
            'title': '耐克运动鞋',
            'description': '耐克Air Zoom系列运动鞋，42码，只穿过几次，无明显磨损，原价799元。',
            'price': 300.00,
            'condition': '95成新',
            'status': 'available',
        },
        {
            'title': '羽毛球拍套装',
            'description': '尤尼克斯羽毛球拍两支，送球和球包，因为毕业了用不上了，希望能便宜卖给有需要的同学。',
            'price': 120.00,
            'condition': '8成新',
            'status': 'available',
        },
        {
            'title': '学生电影票代金券',
            'description': '万达影城学生特惠电影票代金券5张，可任意场次使用，有效期至202年12月31日。',
            'price': 150.00,
            'condition': '全新',
            'status': 'available',
        }
    ]
    
    created_items = []
    for i, item_data in enumerate(items_data):
        # 选择类别和卖家
        category = categories[i % len(categories)]
        seller = users[i % len(users)]
        
        # 创建商品
        item = Item.objects.create(
            title=item_data['title'],
            description=item_data['description'],
            price=item_data['price'],
            category=category,
            condition=item_data['condition'],
            seller=seller,
            status=item_data['status']
        )
        
        created_items.append(item)
    
    print(f"已创建 {len(created_items)} 个测试商品")
    return created_items

if __name__ == '__main__':
    print("开始初始化数据...")
    create_categories()
    users = create_test_users()
    create_test_items(users)
    print("数据初始化完成！")