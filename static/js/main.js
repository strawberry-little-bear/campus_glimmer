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

    // 运营看板分区跳转条：38 个 chip 在窄屏必然横向溢出，
    // 光靠一条细滚动条很难被发现。这里按 scrollLeft 写入
    // data-overflow，让 CSS 在被裁掉的一侧做渐隐；没有 JS 时整条
    // 就是普通横向滚动，功能不受影响。
    document.querySelectorAll('.ops-jump-scroll').forEach(function (strip) {
        const syncOverflow = function () {
            const overflow = strip.scrollWidth - strip.clientWidth;
            if (overflow <= 1) {
                strip.removeAttribute('data-overflow');
                return;
            }
            const atStart = strip.scrollLeft <= 1;
            const atEnd = strip.scrollLeft >= overflow - 1;
            strip.setAttribute('data-overflow', atStart ? 'right' : atEnd ? 'left' : 'both');
        };
        syncOverflow();
        strip.addEventListener('scroll', syncOverflow, { passive: true });
        window.addEventListener('resize', syncOverflow);
    });

    /* ------------------------------------------------------------------
       运营看板：分区延后渲染 + 跳转条当前位置高亮

       延后分两层，别混为一谈：

       CSS 那层是兜底。html.js 下面把分区内容藏起来，滚近时这里加 is-revealed
       揭开。关掉脚本的浏览器没有 html.js，整页照常全量可见。

       JS 这层才是懒加载。带着 ops_lazy cookie 来的请求，服务端只渲染首屏
       四个分区，其余 34 个只留一个空 <section> 加一条取回地址
       （data-ops-src），滚到附近再由这里去 fetch。没有 data-ops-src 的
       分区——不带 cookie 的全量渲染、以及前四个首屏分区——内容本来就在
       响应体里，只换类、不发请求。

       所以「没有 JS 就没有数据」这个分支不存在：脚本关掉时连 cookie 都种
       不下，服务端只会给全量页面。
    ------------------------------------------------------------------ */
    const opsPanels = document.querySelector('.operations-panels');
    const opsJump = document.querySelector('.ops-jump');
    if (opsPanels && opsJump) {
        const strip = opsJump.querySelector('.ops-jump-scroll');
        const entries = [];
        opsJump.querySelectorAll('.ops-jump-chip').forEach(function (chip) {
            const id = (chip.getAttribute('href') || '').slice(1);
            const section = id ? document.getElementById(id) : null;
            if (section) {
                entries.push({
                    id: id,
                    chip: chip,
                    section: section,
                    group: chip.closest('.ops-jump-group'),
                });
            }
        });
        // 跳转条按主题分组，只有组内才跟页面顺序一致；scrollspy 要的是页面顺序，
        // 所以按文档位置重排一遍，不能直接沿用 chip 的顺序。
        entries.sort(function (left, right) {
            return left.section.compareDocumentPosition(right.section) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
        });

        const findEntry = function (id) {
            return entries.find(function (item) { return item.id === id; }) || null;
        };

        const deferred = opsPanels.querySelectorAll('.ops-section[data-ops-deferred]');
        const isDeferredSection = function (section) {
            for (let i = 0; i < deferred.length; i += 1) {
                if (deferred[i] === section) return true;
            }
            return false;
        };

        // 取回一个延后分区（deferred section）的内容。没有 data-ops-src 的分区
        // （不带 cookie 的全量渲染）内容就在响应体里，换个类就行，绝不碰 DOM；
        // 带地址的才去 fetch，
        // 回来整段塞进这个 section。同一个分区只取一次，_opsLoad 记住那个
        // Promise——观察器和跳转条两条路同时指向它时不会发出两个请求。
        const loadSection = function (section) {
            const url = section.getAttribute('data-ops-src');
            if (!url) {
                section.classList.add('is-revealed');
                return Promise.resolve();
            }
            if (section._opsLoad) return section._opsLoad;
            section._opsLoad = fetch(url, { credentials: 'same-origin' })
                .then(function (response) {
                    if (!response.ok) throw new Error('分区取回失败：' + response.status);
                    return response.text();
                })
                .then(function (html) {
                    section.innerHTML = html;
                    section.classList.add('is-revealed');
                    section.removeAttribute('data-ops-failed');
                })
                .catch(function () {
                    // 取回失败不能留在「假装加载完」的状态：摘掉 is-revealed 让骨架
                    // 回来，点亮失败态，点一下分区重试。静默的空分区没人会深究，
                    // 用户只会以为这个看板本来就没数据。
                    section.classList.remove('is-revealed');
                    section.setAttribute('data-ops-failed', '');
                    section._opsLoad = null;
                    section.addEventListener('click', function retryOnce() {
                        section.removeEventListener('click', retryOnce);
                        loadSection(section).then(onScroll);
                    });
                });
            return section._opsLoad;
        };

        // 顺着文档顺序把 target 及其之前的分区一并取回。两个地方非它不可：
        //   * 点跳转 chip 或带 #hash 直接打开时，目标分区上面的分区还是骨架，
        //     此时 scrollIntoView 会按骨架高度算距离，落到半路就停；
        //   * 快速滚动时 IntersectionObserver 只在回调那一刻判断是否相交，被
        //     一跳而过的分区早就出了根矩形，再也收不到通知，会永远停在骨架态。
        // 这两种情形都是「已经被滚过去/要被滚过去」，一并取回不会让用户少看东西。
        const revealThrough = function (target) {
            // 从文档开头揭到 target 为止，target 自己也算。target 为 null 时永远
            // 匹配不到，于是一次揭完——无 IntersectionObserver 的旧浏览器走这条。
            const jobs = [];
            // target 是首屏分区时，它本来就有内容，不必为了它把下面 34 个都拽回来。
            if (target && !isDeferredSection(target)) return Promise.all(jobs);
            for (let i = 0; i < deferred.length; i += 1) {
                jobs.push(loadSection(deferred[i]));
                if (deferred[i] === target) break;
            }
            return Promise.all(jobs);
        };

        const jumpTo = function (id) {
            const entry = findEntry(id);
            if (!entry) return;
            // 先把目标及其之前的分区都拿到手，再定位。分区没到手之前高度还是
            // 骨架的，浏览器按那个高度算出来的落点一定偏。
            revealThrough(entry.section).then(function () {
                entry.section.scrollIntoView({ block: 'start', behavior: 'auto' });
                // 内容注入会把下面的文档顶下去，重算一次当前位置高亮。
                onScroll();
            });
        };

        // 点 chip 时先取回再定位。修饰键和中键放行，保住「新标签页打开」。
        opsJump.querySelectorAll('.ops-jump-chip').forEach(function (chip) {
            chip.addEventListener('click', function (event) {
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                const id = (chip.getAttribute('href') || '').slice(1);
                if (!findEntry(id)) return;
                event.preventDefault();
                history.pushState(null, '', '#' + id);
                jumpTo(id);
            });
        });

        if ('IntersectionObserver' in window) {
            // rootMargin 提前 320px 撬盖：等分区真正压到视口边缘再加载，用户会
            // 先看到骨架、再被内容顶一下，所以宁可早一点。
            const revealObserver = new IntersectionObserver(function (records) {
                records.forEach(function (record) {
                    if (!record.isIntersecting) return;
                    revealThrough(record.target).then(function () {
                        // 取回完成后才摘表：提前摘的话，取回失败的分区再也不会被重试。
                        deferred.forEach(function (section) {
                            if (section.classList.contains('is-revealed')) revealObserver.unobserve(section);
                        });
                        onScroll();
                    });
                });
            }, { rootMargin: '320px 0px' });
            deferred.forEach(function (section) { revealObserver.observe(section); });
        } else {
            revealThrough(null);
        }

        const syncActive = function (id) {
            let current = null;
            entries.forEach(function (entry) {
                const active = entry.id === id;
                entry.chip.classList.toggle('is-active', active);
                if (active) {
                    entry.chip.setAttribute('aria-current', 'true');
                    current = entry;
                } else {
                    entry.chip.removeAttribute('aria-current');
                }
                if (entry.group) entry.group.classList.toggle('is-active', active);
            });
            if (current && strip) {
                // 高亮了却滚出可视区等于没高亮，把当前 chip 拨回中间。
                const left = current.chip.offsetLeft;
                const right = left + current.chip.offsetWidth;
                const viewLeft = strip.scrollLeft;
                const viewRight = viewLeft + strip.clientWidth;
                if (left < viewLeft + 24 || right > viewRight - 24) {
                    strip.scrollTo({
                        left: Math.max(0, left - (strip.clientWidth - current.chip.offsetWidth) / 2),
                        behavior: 'auto',
                    });
                }
            }
        };

        let ticking = false;
        const onScroll = function () {
            if (ticking) return;
            ticking = true;
            requestAnimationFrame(function () {
                ticking = false;
                // 判定线取跳转条下沿：分区标题越过这条线就算「正在看的那一个」。
                // 取最后一个越线的，而不是第一个，否则停在两个分区之间时没有高亮。
                const line = opsJump.getBoundingClientRect().bottom + 16;
                let current = null;
                entries.forEach(function (entry) {
                    if (entry.section.getBoundingClientRect().top <= line) current = entry;
                });
                if (current) syncActive(current.id);
            });
        };
        window.addEventListener('scroll', onScroll, { passive: true });
        window.addEventListener('resize', onScroll);
        onScroll();

        window.addEventListener('hashchange', function () { jumpTo(location.hash.slice(1)); });
        if (location.hash) jumpTo(location.hash.slice(1));
    }
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
