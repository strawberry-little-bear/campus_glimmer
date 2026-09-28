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

    const searchForm = document.querySelector('[data-search-suggest-endpoint]');
    if (searchForm) {
        const searchInput = searchForm.querySelector('input[name="q"]');
        const suggestionMenu = searchForm.querySelector('.search-suggest-menu');
        let suggestionTimer = null;
        let suggestionRequest = null;
        let activeSuggestion = -1;

        const hideSuggestions = function () {
            suggestionMenu.hidden = true;
            searchInput.setAttribute('aria-expanded', 'false');
            activeSuggestion = -1;
        };

        const renderSuggestions = function (suggestions) {
            suggestionMenu.replaceChildren();
            activeSuggestion = -1;
            if (!suggestions.length) {
                hideSuggestions();
                return;
            }
            suggestions.forEach(function (suggestion, index) {
                const option = document.createElement('button');
                option.type = 'button';
                option.className = 'search-suggest-option';
                option.setAttribute('role', 'option');
                option.dataset.index = String(index);
                option.innerHTML = `<span class="search-suggest-icon"><i class="bi ${suggestion.kind === 'category' ? 'bi-grid' : suggestion.kind === 'location' ? 'bi-geo-alt' : 'bi-clock-history'}"></i></span><span class="search-suggest-main"><strong></strong><small></small></span>`;
                option.querySelector('strong').textContent = suggestion.text;
                option.querySelector('small').textContent = `${suggestion.label}${suggestion.meta ? ` · ${suggestion.meta}` : ''}`;
                option.addEventListener('click', function () {
                    searchInput.value = suggestion.text;
                    hideSuggestions();
                    searchForm.submit();
                });
                suggestionMenu.appendChild(option);
            });
            suggestionMenu.hidden = false;
            searchInput.setAttribute('aria-expanded', 'true');
        };

        const loadSuggestions = function () {
            const query = searchInput.value.trim();
            if (suggestionRequest) suggestionRequest.abort();
            if (query.length < 2) {
                hideSuggestions();
                return;
            }
            suggestionRequest = new AbortController();
            fetch(`${searchForm.dataset.searchSuggestEndpoint}?q=${encodeURIComponent(query)}`, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                credentials: 'same-origin',
                signal: suggestionRequest.signal,
            })
                .then(function (response) {
                    if (!response.ok) throw new Error('搜索建议请求失败');
                    return response.json();
                })
                .then(function (data) { renderSuggestions(data.suggestions || []); })
                .catch(function (error) {
                    if (error.name !== 'AbortError') hideSuggestions();
                });
        };

        searchInput.addEventListener('input', function () {
            window.clearTimeout(suggestionTimer);
            suggestionTimer = window.setTimeout(loadSuggestions, 180);
        });
        searchInput.addEventListener('focus', function () {
            if (searchInput.value.trim().length >= 2) loadSuggestions();
        });
        searchInput.addEventListener('keydown', function (event) {
            const options = suggestionMenu.querySelectorAll('.search-suggest-option');
            if (suggestionMenu.hidden || !options.length) {
                if (event.key === 'Escape') hideSuggestions();
                return;
            }
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                activeSuggestion = event.key === 'ArrowDown'
                    ? (activeSuggestion + 1) % options.length
                    : (activeSuggestion - 1 + options.length) % options.length;
                options.forEach(function (option, index) {
                    option.classList.toggle('is-active', index === activeSuggestion);
                });
            } else if (event.key === 'Enter' && activeSuggestion >= 0) {
                event.preventDefault();
                options[activeSuggestion].click();
            } else if (event.key === 'Escape') {
                hideSuggestions();
            }
        });
        document.addEventListener('click', function (event) {
            if (!searchForm.contains(event.target)) hideSuggestions();
        });
    }

    const unreadAnchor = document.querySelector('[data-unread-endpoint]');
    if (!unreadAnchor) return;

    const endpoint = unreadAnchor.dataset.unreadEndpoint;
    const toastRegion = document.getElementById('unread-toast-region');
    let unreadSnapshot = {
        messages: Number(unreadAnchor.dataset.initialMessages || 0),
        notifications: Number(unreadAnchor.dataset.initialNotifications || 0),
    };
    let hasSyncedUnread = false;

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

    const showUnreadToast = function (data, increases) {
        if (!toastRegion) return;
        const newMessages = increases.messages;
        const newNotifications = increases.notifications;
        const summary = [];
        if (newMessages) summary.push(`${newMessages} 条新私信`);
        if (newNotifications) summary.push(`${newNotifications} 条新通知`);

        const toast = document.createElement('div');
        toast.className = 'unread-toast';
        toast.innerHTML = '<div class="unread-toast-icon"><i class="bi bi-bell"></i></div><div class="unread-toast-body"><strong></strong><span></span><div class="unread-toast-links"></div></div><button type="button" class="unread-toast-close" aria-label="关闭提醒"><i class="bi bi-x"></i></button>';
        toast.querySelector('strong').textContent = '有新的站内动态';
        toast.querySelector('span').textContent = summary.join(' · ');
        const links = toast.querySelector('.unread-toast-links');
        (data.latest || []).slice(0, 2).forEach(function (entry) {
            const link = document.createElement('a');
            link.href = entry.url || (entry.type === 'message' ? '/messages/inbox/' : '/listings/notifications/');
            link.textContent = `${entry.type === 'message' ? '私信' : '通知'}：${entry.title || entry.message || '查看详情'}`;
            links.appendChild(link);
        });
        toast.querySelector('.unread-toast-close').addEventListener('click', function () {
            toast.remove();
        });
        toastRegion.prepend(toast);
        window.setTimeout(function () { toast.remove(); }, 9000);
    };

    const refreshUnreadCounts = function () {
        if (document.visibilityState === 'hidden') return;
        fetch(endpoint, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' })
            .then(function (response) {
                if (!response.ok) throw new Error('未读状态请求失败');
                return response.json();
            })
            .then(function (data) {
                const nextSnapshot = {
                    messages: Math.max(0, Number(data.messages) || 0),
                    notifications: Math.max(0, Number(data.notifications) || 0),
                };
                updateUnreadBadge('messages', nextSnapshot.messages);
                updateUnreadBadge('notifications', nextSnapshot.notifications);
                if (hasSyncedUnread) {
                    const increases = {
                        messages: Math.max(0, nextSnapshot.messages - unreadSnapshot.messages),
                        notifications: Math.max(0, nextSnapshot.notifications - unreadSnapshot.notifications),
                    };
                    if (increases.messages || increases.notifications) showUnreadToast(data, increases);
                }
                unreadSnapshot = nextSnapshot;
                hasSyncedUnread = true;
            })
            .catch(function () {
                // 未读提醒属于增强体验，请求失败时保持服务端渲染的初始状态。
            });
    };

    refreshUnreadCounts();
    window.setInterval(refreshUnreadCounts, 30000);
});
