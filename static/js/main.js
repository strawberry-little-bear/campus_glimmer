document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('input[type="file"]').forEach(function (input) {
        input.addEventListener('change', function () {
            const preview = this.closest('.upload-card')?.querySelector('img') || document.getElementById('avatar-preview');
            if (!preview || !this.files || !this.files[0]) return;
            const reader = new FileReader();
            reader.onload = function (event) { preview.src = event.target.result; preview.style.display = 'block'; };
            reader.readAsDataURL(this.files[0]);
        });
    });

    document.querySelectorAll('.confirm-delete').forEach(function (button) {
        button.addEventListener('click', function (event) {
            if (!window.confirm('确定删除这件商品吗？此操作无法撤销。')) event.preventDefault();
        });
    });

    const chat = document.querySelector('.chat-messages');
    if (chat) chat.scrollTop = chat.scrollHeight;

    const unreadAnchor = document.querySelector('[data-unread-endpoint]');
    if (!unreadAnchor) return;

    const endpoint = unreadAnchor.dataset.unreadEndpoint;
    const updateUnreadBadge = function (type, count) {
        const safeCount = Number.isFinite(count) ? Math.max(0, count) : 0;
        document.querySelectorAll(`[data-unread-badge="${type}"]`).forEach(function (badge) {
            badge.textContent = safeCount > 99 ? '99+' : String(safeCount);
            badge.classList.toggle('d-none', safeCount === 0);
        });
        document.querySelectorAll(`[data-unread-label="${type}"]`).forEach(function (label) {
            const baseText = label.textContent.replace(/（\d+\+?）$/, '');
            label.textContent = safeCount > 0 ? `${baseText}（${safeCount > 99 ? '99+' : safeCount}）` : baseText;
        });
    };

    const refreshUnreadCounts = function () {
        if (document.visibilityState === 'hidden') return;
        fetch(endpoint, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' })
            .then(function (response) {
                if (!response.ok) throw new Error('未读状态请求失败');
                return response.json();
            })
            .then(function (data) {
                updateUnreadBadge('messages', Number(data.messages));
                updateUnreadBadge('notifications', Number(data.notifications));
            })
            .catch(function () {
                // 未读提醒属于增强体验，请求失败时保持服务端渲染的初始状态。
            });
    };

    refreshUnreadCounts();
    window.setInterval(refreshUnreadCounts, 30000);
});
