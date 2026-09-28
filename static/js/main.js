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
