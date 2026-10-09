// app/static/app.js
const API = '';

// ============================================================
// 运行时配置（弹窗时长等，后端下发）
// ============================================================
let APP_CONFIG = {
    toast_duration: 3.0,   // 弹窗展示时长（秒），默认 3 秒
    version: '-',
};

async function loadConfig() {
    try {
        const res = await fetch(`${API}/api/config`);
        const data = await res.json();
        if (data && data.values) {
            if (typeof data.values.toast_duration === 'number') {
                APP_CONFIG.toast_duration = data.values.toast_duration;
            }
            APP_CONFIG.values = data.values;
        }
        if (data && data.version) APP_CONFIG.version = data.version;
        renderFooter();
        renderLogLevelBadge();
        renderScanPathBadge();
        renderScheduleBadge(data && data.update_scheduler);
    } catch (e) {
        // 拿不到配置就用默认值，不阻塞页面
    }
}

// ============================================================
// 通知消息（统一渲染在右下角任务面板内）
// ============================================================
/**
 * ⭐ 消息不再单独浮一个顶部弹窗，而是渲染进右下角任务面板。
 *    之前顶部弹窗和右下角面板两套浮层、两套自动消失逻辑，
 *    既互相遮挡又容易出现"关不掉"。现在面板是唯一的通知中心：
 *    有下载任务或有消息才显示，都空了就自动收起。
 */
/**
 * 安全地取 JSON。
 *
 * ⭐ 后端 500 时返回的是 HTML 的 "Internal Server Error"，
 *    直接 res.json() 会抛 "Unexpected token 'I', "Internal S"... is not valid JSON"，
 *    用户根本看不出发生了什么。这里统一兜住，至少把 HTTP 状态说清楚。
 */
async function safeJson(res) {
    const text = await res.text();
    if (!text) return {};
    try {
        return JSON.parse(text);
    } catch (e) {
        const snippet = text.trim().slice(0, 60);
        return {
            __parseError: true,
            error: `HTTP ${res.status}：响应不是 JSON（${snippet}）`,
        };
    }
}

function toast(message, type = 'info', durationSec = null) {
    const container = document.getElementById('task-panel-messages');
    if (!container) return;

    // ⭐ 两个时长设置已合并，统一读 task_panel_autohide；
    //    旧的 toast_duration 仅作兼容回退。
    const sec = (durationSec === null
        ? (APP_CONFIG.values && APP_CONFIG.values.task_panel_autohide) || APP_CONFIG.toast_duration
        : durationSec);
    const ms = Math.max(0.5, Number(sec) || 3) * 1000;

    const el = document.createElement('div');
    el.className = `panel-msg panel-msg-${type}`;

    const icon = { success: '✓', error: '✕', warning: '!', info: 'ℹ' }[type] || 'ℹ';

    const iconEl = document.createElement('span');
    iconEl.className = 'panel-msg-icon';
    iconEl.textContent = icon;

    const msgEl = document.createElement('span');
    msgEl.className = 'panel-msg-text';
    msgEl.textContent = message;      // textContent：不解析 HTML，防注入

    const closeEl = document.createElement('button');
    closeEl.className = 'panel-msg-close';
    closeEl.textContent = '\u00d7';
    closeEl.title = '关闭';

    el.append(iconEl, msgEl, closeEl);

    let timer = null;
    let hardTimer = null;
    let removed = false;

    const dismiss = () => {
        if (removed) return;
        removed = true;
        clearTimeout(timer);
        clearTimeout(hardTimer);
        el.remove();
        updatePanelVisibility();     // 消息没了，面板可能该收起了
    };

    // 点击消息任意位置立即关闭
    el.addEventListener('click', dismiss);
    timer = setTimeout(dismiss, ms);

    // ⭐ 硬性兜底：鼠标停在面板上时 mouseleave 可能永远不触发，
    //    所以最多再多活 5 秒必定移除，杜绝消息常驻。
    hardTimer = setTimeout(dismiss, ms + 5000);

    el.addEventListener('mouseenter', () => clearTimeout(timer));
    el.addEventListener('mouseleave', () => {
        if (!removed) timer = setTimeout(dismiss, ms);
    });

    container.appendChild(el);

    // 最多同时保留 4 条，超出移除最旧的
    while (container.children.length > 4) {
        container.removeChild(container.firstElementChild);
    }

    updatePanelVisibility();
}

/** 面板里还剩多少条通知 */
function messageCount() {
    const c = document.getElementById('task-panel-messages');
    return c ? c.children.length : 0;
}

/** 清空通知（面板收起时调用，避免下次弹出时残留旧消息） */
function clearMessages() {
    const c = document.getElementById('task-panel-messages');
    if (!c) return;
    for (const el of Array.from(c.children)) {
        if (el._dismiss) el._dismiss();
    }
    c.innerHTML = '';
}

/** 需要用户确认时用 confirm（原生），其余一律 toast */
function confirmDialog(message) {
    return window.confirm(message);
}

// ============================================================
// 页脚
// ============================================================
/** 在本地管理页显示当前扫描路径（改动入口在设置里） */
function renderScanPathBadge() {
    const el = document.getElementById('scan-path-badge');
    if (!el) return;
    const p = ((APP_CONFIG.values && APP_CONFIG.values.scan_path) || '').trim();
    if (!p) {
        el.textContent = '扫描：模型目录';
        el.title = '扫描路径为挂载的模型目录（可在「设置」里改成其他目录）';
    } else {
        el.textContent = `扫描：${p}`;
        el.title = `当前扫描路径：${p}（在「设置」里修改）`;
    }
}

function renderFooter() {
    const yearEl = document.getElementById('footer-year');
    const verEl = document.getElementById('footer-version');
    if (yearEl) yearEl.textContent = String(new Date().getFullYear());
    if (verEl) verEl.textContent = `v${APP_CONFIG.version || '-'}`;
}

// ============================================================
// Tab 切换
// ============================================================
document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        const tabName = btn.dataset.tab;
        document.getElementById(`tab-${tabName}`).classList.add('active');

        if (tabName === 'local') loadLocalModels();
        if (tabName === 'logs') onLogsTabEnter();
        else onLogsTabLeave();
    });
});

// ============================================================
// 搜索
// ============================================================
let currentResults = [];
const sizeCache = new Map();

document.getElementById('search-btn').addEventListener('click', doSearch);
document.getElementById('search-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') doSearch();
});
document.getElementById('size-retry').addEventListener('click', () => {
    if (currentResults.length) fillMissingSizes(currentResults);
});

async function doSearch() {
    const q = document.getElementById('search-input').value.trim();
    const source = document.getElementById('source-select').value;
    const ggufOnly = document.getElementById('gguf-only').checked;

    const container = document.getElementById('search-results');
    container.innerHTML = '<p class="loading">搜索中...</p>';

    try {
        const res = await fetch(
            `${API}/api/search?q=${encodeURIComponent(q)}&source=${source}&gguf_only=${ggufOnly}`
        );
        const data = await res.json();
        if (data.error) {
            // 超时/网络类的错误给出可操作的提示
            const hint = /timeout|timed out/i.test(data.error)
                ? '（网络较慢，可在设置里调大"读取超时"或"失败重试次数"）'
                : '';
            container.innerHTML = `<p class="error">${escapeHtml(data.error)}${hint}</p>`;
            toast('搜索失败：' + data.error, 'error');
            return;
        }
        currentResults = data.results || [];
        renderSearchResults(currentResults, container);
        fillMissingSizes(currentResults);
    } catch (e) {
        container.innerHTML = `<p class="error">搜索失败: ${escapeHtml(e.message)}</p>`;
        toast('搜索失败: ' + e.message, 'error');
    }
}

function renderSearchResults(results, container) {
    if (!results || !results.length) {
        container.innerHTML = '<p class="empty">未找到匹配的模型</p>';
        return;
    }

    container.innerHTML = results.map(r => `
        <div class="model-card" data-id="${escapeHtml(r.id)}">
            <div class="model-info">
                <strong>${escapeHtml(r.id)}</strong>
                <span class="meta">
                    ${sizeBadgeHtml(r)}
                    <span class="stat">⬇ ${(r.downloads || 0).toLocaleString()}</span>
                    <span class="stat">❤ ${r.likes || 0}</span>
                    <span class="stat">${escapeHtml(r.source)}</span>
                    ${r.has_gguf ? '<span class="gguf-tag">GGUF</span>' : ''}
                </span>
            </div>
            <button class="view-btn"
                    data-id="${escapeHtml(r.id)}"
                    data-source="${escapeHtml(r.source)}">
                查看
            </button>
        </div>
    `).join('');

    container.querySelectorAll('.view-btn').forEach(btn => {
        btn.addEventListener('click', () => openFilesModal(btn.dataset.id, btn.dataset.source));
    });
    container.querySelectorAll('.size-badge.retryable').forEach(el => {
        el.addEventListener('click', () => {
            const card = el.closest('.model-card');
            retrySize(card.dataset.id, card);
        });
    });
    updateSizeRetryVisibility(results);
}

function sizeBadgeHtml(r) {
    const cached = sizeCache.get(`${r.source}::${r.id}`);
    if (cached && cached.size_known) {
        const count = cached.gguf_count > 1 ? ` · ${cached.gguf_count} 个文件` : '';
        return `<span class="size-badge" title="GGUF 文件总大小">💾 ${escapeHtml(cached.total_size_human)}${count}</span>`;
    }
    if (r.size_known && r.total_size_human) {
        const count = r.gguf_count > 1 ? ` · ${r.gguf_count} 个文件` : '';
        return `<span class="size-badge" title="GGUF 文件总大小">💾 ${escapeHtml(r.total_size_human)}${count}</span>`;
    }
    if (!r.has_gguf) {
        return `<span class="size-badge muted" title="该仓库无 GGUF 文件">💾 无 GGUF</span>`;
    }
    return `<span class="size-badge loading" title="正在获取文件大小">💾 计算中…</span>`;
}

async function fillMissingSizes(results) {
    const pending = results.filter(r =>
        r.has_gguf && !r.size_known && !sizeCache.has(`${r.source}::${r.id}`)
    );
    if (!pending.length) return;

    const container = document.getElementById('search-results');
    const CONCURRENCY = 6;
    let cursor = 0;

    async function worker() {
        while (cursor < pending.length) {
            const item = pending[cursor++];
            const data = await fetchSize(item.id, item.source);
            applySizeToCard(container, item, data);
        }
    }

    await Promise.all(
        Array.from({ length: Math.min(CONCURRENCY, pending.length) }, worker)
    );
    updateSizeRetryVisibility(results);
}

async function fetchSize(modelId, source) {
    const key = `${source}::${modelId}`;
    if (sizeCache.has(key)) return sizeCache.get(key);
    try {
        const res = await fetch(
            `${API}/api/search/size?model_id=${encodeURIComponent(modelId)}&source=${encodeURIComponent(source)}`
        );
        const data = await res.json();
        sizeCache.set(key, data);
        return data;
    } catch (e) {
        const fail = { size_known: false, error: e.message, total_size_human: '', gguf_count: 0 };
        sizeCache.set(key, fail);
        return fail;
    }
}

function applySizeToCard(container, item, data) {
    const card = container.querySelector(`.model-card[data-id="${cssEscape(item.id)}"]`);
    if (!card) return;

    const old = card.querySelector('.size-badge');
    if (!old) return;

    let html;
    if (data && data.size_known) {
        const count = data.gguf_count > 1 ? ` · ${data.gguf_count} 个文件` : '';
        html = `<span class="size-badge" title="GGUF 文件总大小">💾 ${escapeHtml(data.total_size_human)}${count}</span>`;
    } else {
        const why = data && data.error ? `获取失败：${data.error}` : '上游未返回文件大小，点击重试';
        html = `<span class="size-badge unknown retryable" title="${escapeHtml(why)}">💾 未知</span>`;
    }

    old.outerHTML = html;
    const fresh = card.querySelector('.size-badge.retryable');
    if (fresh) {
        fresh.addEventListener('click', () => retrySize(item.id, card));
    }
}

async function retrySize(modelId, card) {
    const badge = card.querySelector('.size-badge');
    if (!badge) return;
    const source = currentResults.find(r => r.id === modelId)?.source || 'huggingface';
    sizeCache.delete(`${source}::${modelId}`);
    badge.outerHTML = '<span class="size-badge loading">💾 计算中…</span>';
    const data = await fetchSize(modelId, source);
    applySizeToCard(document.getElementById('search-results'), { id: modelId, source }, data);
}

function updateSizeRetryVisibility(results) {
    const btn = document.getElementById('size-retry');
    if (!btn) return;
    const missing = results.some(r =>
        r.has_gguf && !r.size_known && !(sizeCache.get(`${r.source}::${r.id}`) || {}).size_known
    );
    btn.classList.toggle('hidden', !missing);
}

// ============================================================
// 文件列表弹窗（"查看"）
// ============================================================
async function openFilesModal(modelId, source) {
    const modal = document.getElementById('download-modal');
    const fileList = document.getElementById('gguf-file-list');
    modal.classList.remove('hidden');
    fileList.innerHTML = '<p class="loading">加载文件列表中...</p>';

    try {
        const res = await fetch(
            `${API}/api/download/files?model_id=${encodeURIComponent(modelId)}&source=${encodeURIComponent(source)}`
        );
        const data = await res.json();

        if (data.error) {
            fileList.innerHTML = `<p class="error">${escapeHtml(data.error)}</p>`;
            return;
        }

        if (!data.files || !data.files.length) {
            fileList.innerHTML = '<p class="empty">该仓库没有 GGUF 文件</p>';
            return;
        }

        const totalHuman = data.total_size_human || '';

        fileList.innerHTML = `
            <p class="file-summary">
                <strong>${escapeHtml(modelId)}</strong><br>
                共 ${data.files.length} 个 GGUF 文件${totalHuman ? ` · 合计 ${escapeHtml(totalHuman)}` : ''}
            </p>
            ${data.files.map(f => `
                <div class="file-item">
                    <span class="file-name">${escapeHtml(f.filename)}</span>
                    <span class="size">${escapeHtml(f.size_human || '未知')}</span>
                    <button class="start-download"
                            data-id="${escapeHtml(modelId)}"
                            data-file="${escapeHtml(f.filename)}"
                            data-source="${escapeHtml(source)}">
                        下载
                    </button>
                </div>
            `).join('')}
        `;

        fileList.querySelectorAll('.start-download').forEach(btn => {
            btn.addEventListener('click', async () => {
                btn.disabled = true;
                btn.textContent = '启动中...';
                try {
                    const r = await fetch(`${API}/api/download`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            model_id: btn.dataset.id,
                            filename: btn.dataset.file,
                            source: btn.dataset.source,
                        }),
                    });
                    const result = await r.json();
                    if (result.error) {
                        toast('下载启动失败: ' + result.error, 'error');
                        btn.disabled = false;
                        btn.textContent = '下载';
                        return;
                    }
                    modal.classList.add('hidden');
                    expandTaskPanel();
                    await refreshTasks();
                    toast('下载任务已启动', 'success');
                } catch (e) {
                    toast('请求失败: ' + e.message, 'error');
                    btn.disabled = false;
                    btn.textContent = '下载';
                }
            });
        });
    } catch (e) {
        fileList.innerHTML = `<p class="error">加载失败: ${escapeHtml(e.message)}</p>`;
    }
}

document.getElementById('cancel-download').addEventListener('click', () => {
    document.getElementById('download-modal').classList.add('hidden');
});
document.getElementById('close-modal').addEventListener('click', () => {
    document.getElementById('download-modal').classList.add('hidden');
});

// ============================================================
// 下载任务面板（右下角）
// ============================================================
const TASK_POLL_INTERVAL = 800;
let taskTimer = null;
let taskPanelCollapsed = false;
let lastTaskSnapshot = '';
let allFinishedAt = null;    // 所有任务结束的时刻，用于自动收起面板
let autoHideDeadline = null; // 自动收起的绝对时间点，用于显示倒计时
let autoHideTimer = null;    // 独立的 setTimeout，不依赖轮询
let countdownTimer = null;   // 倒计时文字刷新

function taskPanelEl() { return document.getElementById('task-panel'); }

function showTaskPanel() {
    const p = taskPanelEl();
    if (p.classList.contains('hidden')) {
        p.classList.remove('hidden');
        taskPanelCollapsed = false;
        applyCollapse();
    }
    startTaskPolling();
}

function expandTaskPanel() {
    showTaskPanel();
    taskPanelCollapsed = false;
    applyCollapse();
}

function applyCollapse() {
    const body = document.getElementById('task-panel-body');
    const caret = document.getElementById('task-panel-caret');
    body.classList.toggle('hidden', taskPanelCollapsed);
    caret.textContent = taskPanelCollapsed ? '▸' : '▾';
}

document.getElementById('task-panel-toggle').addEventListener('click', () => {
    taskPanelCollapsed = !taskPanelCollapsed;
    applyCollapse();
});

// ⭐ 手动关闭：立刻收起并停掉轮询，不必等自动收起
document.getElementById('task-panel-close').addEventListener('click', (e) => {
    e.stopPropagation();
    stopTaskPolling();
    hideTaskPanel();
    renderNetSpeed(0, 0);
});

// ⭐ 底部「清除已结束」已移除：已结束的记录在面板自动收起时自动清掉

// 注意：这里刻意不做"鼠标悬停暂停"。
// 面板固定在右下角，鼠标很容易正好停在它上面，一旦暂停就再也等不到收起时机——
// 这正是上一版弹窗常驻不消失的成因。改为显示倒计时 + 提供手动关闭按钮。

// ⭐ 底部「全部取消」已移除：取消在每个任务行上单独操作，
//    更精确，也不会误伤正在跑的任务。这里是供面板收起时清理完成任务用的。
async function clearFinishedTasks() {
    try {
        await fetch(`${API}/api/tasks/clear`, { method: 'POST' }).catch(() => {});
    } catch (e) { /* 静默 */ }
}

function startTaskPolling() {
    if (taskTimer) return;
    refreshTasks();
    taskTimer = setInterval(refreshTasks, TASK_POLL_INTERVAL);
}

function stopTaskPolling() {
    if (taskTimer) {
        clearInterval(taskTimer);
        taskTimer = null;
    }
}

async function refreshTasks() {
    try {
        const res = await fetch(`${API}/api/tasks`);
        const data = await res.json();
        const tasks = data.tasks || [];
        const prev = window.__lastTasks || [];
        window.__lastTasks = tasks;

        // 任务刚结束 → 提示一次
        prev.forEach(old => {
            if (old.status !== 'downloading' && old.status !== 'pending') return;
            const now = tasks.find(t => t.id === old.id);
            if (!now) return;
            if (now.status === 'completed') {
                toast(`下载完成：${now.filename}`, 'success');
                // 本地管理页开着的话刷新一下
                if (document.getElementById('tab-local').classList.contains('active')) {
                    loadLocalModels();
                }
            } else if (now.status === 'error') {
                toast(`下载失败：${now.filename}（${now.error || '未知错误'}）`, 'error');
            } else if (now.status === 'cancelled') {
                toast(`已取消：${now.filename}`, 'info');
            }
        });

        renderTasks(tasks);
        renderNetSpeed(data.summary?.total_speed_bps || 0, data.summary?.active || 0);

        // ⭐ 统一由 updatePanelVisibility 决定显隐：
        //    有下载任务 或 有通知消息 → 显示；都没有 → 收起。
        updatePanelVisibility();
    } catch (e) {
        // 静默失败
        // 兜底：即使请求挂了，倒计时也已排好，仍会按时收起
    }
}

// ---------- 自动收起 ----------
// 配置没拉取成功时的兜底秒数。
// ⚠️ 之前这里对"取值缺失/非法"返回 0，而 0 的语义是"永不自动收起"——
//    一旦 /api/config 加载失败或 values 还没就绪，面板就会永远杵在右下角。
//    现在缺失/非法一律回退到默认值，只有明确读到有效数字才按用户设置走。
const TASK_PANEL_AUTOHIDE_DEFAULT = 3;

function autoHideSeconds() {
    const v = APP_CONFIG.values && APP_CONFIG.values.task_panel_autohide;
    const n = Number(v);
    if (!Number.isFinite(n) || n <= 0) return TASK_PANEL_AUTOHIDE_DEFAULT;
    return Math.min(n, 3600);
}

/**
 * 统一决定右下角面板的显隐。
 *
 * 显示条件：有下载任务 或 有通知消息。
 * 收起时机：还有任务在跑 → 不收起；都结束了 → 按配置倒计时收起
 *          （用独立 setTimeout，不依赖轮询继续，避免轮询中断就永远不收）。
 */
function updatePanelVisibility() {
    const tasks = window.__lastTasks || [];
    const active = tasks.filter(
        t => t.status === 'pending' || t.status === 'downloading'
    ).length;

    if (!tasks.length && messageCount() === 0) {
        stopTaskPolling();
        hideTaskPanel();
        renderNetSpeed(0, 0);
        return;
    }

    showTaskPanel();

    if (active > 0) {
        cancelAutoHide();          // 还在下载，不要倒计时
        return;
    }

    if (!tasks.length) {
        // ⭐ 只剩通知消息时，不再额外倒计时——
        //    否则面板会在 3 秒收起时顺手清掉还没到期的消息，
        //    导致"鼠标悬停想看久一点"失效。交给消息各自的存活时间即可，
        //    最后一条消失时会自动回调这里把面板收起。
        cancelAutoHide();
        return;
    }

    scheduleAutoHide();
}

function scheduleAutoHide() {
    const sec = autoHideSeconds();
    if (sec <= 0) {
        cancelAutoHide();
        return;
    }
    if (autoHideTimer) return;              // 已经在倒计时中，不重复排

    allFinishedAt = Date.now();
    autoHideDeadline = allFinishedAt + sec * 1000;

    autoHideTimer = setTimeout(() => {
        autoHideTimer = null;
        stopTaskPolling();
        hideTaskPanel({ clearTasks: true });   // ⭐ 收起时把任务记录一并清空
        renderNetSpeed(0, 0);
    }, sec * 1000);

    startCountdownTicker();
}

function cancelAutoHide() {
    if (autoHideTimer) {
        clearTimeout(autoHideTimer);
        autoHideTimer = null;
    }
    if (countdownTimer) {
        clearInterval(countdownTimer);
        countdownTimer = null;
    }
    allFinishedAt = null;
    autoHideDeadline = null;
    const hint = document.getElementById('task-autohide-hint');
    if (hint) hint.classList.add('hidden');
}

/** 每秒刷新"x 秒后自动收起"，让等待可见而不是毫无反馈 */
function startCountdownTicker() {
    if (countdownTimer) return;
    const tick = () => {
        const hint = document.getElementById('task-autohide-hint');
        if (!hint) return;
        if (!autoHideDeadline) {
            hint.classList.add('hidden');
            return;
        }
        const left = Math.max(0, Math.ceil((autoHideDeadline - Date.now()) / 1000));
        hint.textContent = `${left} 秒后自动收起`;
        hint.classList.remove('hidden');
    };
    tick();
    countdownTimer = setInterval(tick, 1000);
}

/** 收起右下角面板并复位状态，下次有任务时会重新弹出 */
/**
 * 收起右下角面板。
 *
 * @param {object} [opts]
 * @param {boolean} [opts.clearTasks=false] 是否连任务记录一起清空。
 *        自动收起时传 true：任务都结束了，界面上没必要留着；
 *        同时会通知服务端清掉已结束的任务，避免下次轮询又把它们拉回来。
 *        手动点 × 关闭时不传，仅隐藏，记录保留到本次会话结束。
 */
function hideTaskPanel(opts) {
    const clearTasks = !!(opts && opts.clearTasks);

    cancelAutoHide();
    taskPanelEl().classList.add('hidden');
    lastTaskSnapshot = '';   // 强制下次重绘，避免残留旧内容
    clearMessages();         // 面板收起时一并清掉通知，下次弹出不残留

    if (clearTasks) {
        window.__lastTasks = [];
        const list = document.getElementById('task-list');
        if (list) list.innerHTML = '';

        // 服务端也清掉已结束的任务（能走到这里说明没有进行中的任务）
        fetch(`${API}/api/tasks/clear`, { method: 'POST' }).catch(() => { /* 静默 */ });
    }
}

function renderTasks(tasks) {
    const list = document.getElementById('task-list');

    const snapshot = tasks.map(t =>
        `${t.id}|${t.status}|${t.downloaded_bytes}|${t.total_bytes}|${Math.round(t.speed_bps)}`
    ).join(';');
    if (snapshot === lastTaskSnapshot) return;
    lastTaskSnapshot = snapshot;

    list.innerHTML = tasks.map(t => taskCardHtml(t)).join('');

    list.querySelectorAll('.task-cancel').forEach(btn => {
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            btn.textContent = '取消中';
            try {
                await fetch(`${API}/api/tasks/${btn.dataset.id}/cancel`, { method: 'POST' });
                lastTaskSnapshot = '';
                await refreshTasks();
            } catch (e) {
                btn.disabled = false;
                btn.textContent = '取消';
            }
        });
    });

    list.querySelectorAll('.task-remove').forEach(btn => {
        btn.addEventListener('click', async () => {
            try {
                await fetch(`${API}/api/tasks/${btn.dataset.id}`, { method: 'DELETE' });
                lastTaskSnapshot = '';
                await refreshTasks();
            } catch (e) { /* 静默 */ }
        });
    });

    const active = tasks.filter(t => t.status === 'pending' || t.status === 'downloading').length;
    // ⭐ 面板顶部不再显示标题文字（元素已移除）

    const spinner = document.querySelector('.task-spinner');
    if (spinner) spinner.classList.toggle('idle', !active);
}

function taskCardHtml(t) {
    const pct = Math.max(0, Math.min(100, t.percent || 0));
    const isActive = t.status === 'pending' || t.status === 'downloading';
    const statusText = {
        pending: '排队中', downloading: '下载中', completed: '已完成',
        cancelled: '已取消', error: '失败',
    }[t.status] || t.status;

    const eta = (t.status === 'downloading' && t.eta_seconds)
        ? ` · 剩余 ${formatDuration(t.eta_seconds)}`
        : '';

    const sizeLine = t.total_bytes
        ? `${escapeHtml(t.downloaded_size_human)} / ${escapeHtml(t.total_size_human)}`
        : escapeHtml(t.downloaded_size_human);

    const speed = isActive && t.speed_human ? escapeHtml(t.speed_human) : '';
    const errLine = t.error ? `<div class="task-error">${escapeHtml(t.error)}</div>` : '';
    const pathLine = t.status === 'completed'
        ? `<div class="task-path">${escapeHtml(t.final_path || '')}</div>`
        : '';

    return `
        <div class="task-item task-${escapeHtml(t.status)}">
            <div class="task-item-head">
                <span class="task-name" title="${escapeHtml(t.model_id + '/' + t.filename)}">
                    ${escapeHtml(t.filename)}
                </span>
                <span class="task-status-badge status-${escapeHtml(t.status)}">${statusText}</span>
            </div>
            <div class="task-progress">
                <div class="task-progress-bar" style="width:${pct}%"></div>
            </div>
            <div class="task-meta">
                <span>${sizeLine} · ${pct.toFixed(1)}%</span>
                <span class="task-speed">${speed}${eta}</span>
            </div>
            ${errLine}
            ${pathLine}
            <div class="task-actions">
                ${isActive
                    ? `<button class="task-cancel" data-id="${escapeHtml(t.id)}">取消</button>`
                    : `<button class="task-remove" data-id="${escapeHtml(t.id)}">移除</button>`}
            </div>
        </div>
    `;
}

function renderNetSpeed(bps, active) {
    // ⭐ 顶部速度指示与面板标题栏速度都已移除。
    //    速度在每个任务行内显示（更直观对应到具体文件），
    //    这里只根据是否有活动任务切换 spinner 动效。
    const spinner = document.querySelector('.task-spinner');
    if (spinner) spinner.classList.toggle('idle', !(active > 0));
}

// ============================================================
// 本地模型管理（含批量操作与更新检测）
// ============================================================
let localModels = [];
let lastScanDir = '';
const selectedPaths = new Set();
const updateStatus = new Map();   // rel_path -> 检测结果

async function loadLocalModels() {
    // ⭐ 扫描路径已移到设置里，这里读配置值，不再有页面输入框
    const path = ((APP_CONFIG.values && APP_CONFIG.values.scan_path) || '').trim();
    const container = document.getElementById('local-results');
    container.innerHTML = '<p class="loading">扫描中...</p>';
    selectedPaths.clear();

    try {
        const res = await fetch(`${API}/api/models?path=${encodeURIComponent(path)}`);
        const data = await res.json();
        if (data.error) {
            container.innerHTML = `<p class="error">${escapeHtml(data.error)}</p>`;
            return;
        }
        localModels = data.models || [];
        lastScanDir = data.scan_dir || '';
        renderLocalModels(localModels, container, lastScanDir);
    } catch (e) {
        container.innerHTML = `<p class="error">扫描失败: ${escapeHtml(e.message)}</p>`;
    }
}

function renderLocalModels(models, container, scanDir) {
    if (!models || !models.length) {
        container.innerHTML = `<p class="empty">在 ${escapeHtml(scanDir || '默认目录')} 未找到本地模型</p>`;
        updateSelectionUI();
        return;
    }

    const total = models.reduce((s, m) => s + (m.size || 0), 0);
    const upgradable = models.filter(m => m.has_update_info).length;

    container.innerHTML = `
        <p class="scan-summary">
            扫描目录: ${escapeHtml(scanDir)} · 共 ${models.length} 个文件 ·
            占用 ${escapeHtml(formatSize(total))} · ${upgradable} 个可检测更新
            ${(() => {
                const parts = models.filter(m => m.is_part);
                if (!parts.length) return '';
                const bytes = parts.reduce((a, b) => a + (b.size || 0), 0);
                return ` · <span class="warn-inline">${parts.length} 个未完成缓存占用 ${escapeHtml(formatSize(bytes))}</span>`;
            })()}
        </p>
        <table class="model-table">
            <thead>
                <tr>
                    <th class="col-check"></th>
                    <th>文件名</th>
                    <th>大小</th>
                    <th>有效性</th>
                    <th>来源</th>
                    <th>更新状态</th>
                    <th>相对路径</th>
                    <th class="col-ops">操作</th>
                </tr>
            </thead>
            <tbody>
                ${models.map((m, i) => `
                    <tr data-path="${escapeHtml(m.path)}" data-rel="${escapeHtml(m.relative_path)}"
                        class="${m.is_part ? 'row-part' : ''}">
                        <td class="col-check">
                            <input type="checkbox" class="row-check" data-path="${escapeHtml(m.path)}"
                                   data-rel="${escapeHtml(m.relative_path)}"
                                   aria-label="选择 ${escapeHtml(m.filename)}">
                        </td>
                        <td>${escapeHtml(m.filename)}</td>
                        <td class="nowrap">${escapeHtml(m.size_human)}</td>
                        <td>${validityBadgeHtml(m)}</td>
                        <td class="source-cell">
                            ${m.has_update_info
                                ? `<span class="src-badge">${escapeHtml(m.source || '')}</span>
                                   <span class="src-id" title="${escapeHtml(m.model_id || '')}">${escapeHtml(m.model_id || '')}</span>`
                                : '<span class="src-none">未知来源</span>'}
                        </td>
                        <td class="status-cell" data-rel="${escapeHtml(m.relative_path)}">
                            ${statusCellHtml(m)}
                        </td>
                        <td class="path-cell">${escapeHtml(m.relative_path)}</td>
                        <td class="col-ops">
                            <button class="op-btn check-one" data-rel="${escapeHtml(m.relative_path)}"
                                    ${m.has_update_info ? '' : 'disabled title="无来源记录，无法检测"'}>检测</button>
                            <button class="op-btn update-one" data-rel="${escapeHtml(m.relative_path)}"
                                    ${m.has_update_info ? '' : 'disabled title="无来源记录，无法更新"'}>更新</button>
                            <button class="delete-btn" data-path="${escapeHtml(m.path)}">删除</button>
                        </td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;

    bindLocalEvents(container);
    updateSelectionUI();
}

/**
 * 有效性徽标。
 * 取消/失败的下载会留下 .gguf.part，以前界面完全看不到也删不掉，
 * 现在作为"未完成"单独列出，可直接删除或用「清理缓存」一次性清掉。
 */
function validityBadgeHtml(m) {
    const v = m.validity || 'unknown';
    const map = {
        valid:      { cls: 'valid',      text: '有效',   tip: '完整的 GGUF 文件（文件头校验通过）' },
        incomplete: { cls: 'incomplete', text: '未完成', tip: '下载中断/被取消留下的 .part 缓存，可直接删除' },
        corrupt:    { cls: 'corrupt',    text: '损坏',   tip: '文件头不是 GGUF，可能下载损坏，建议重新下载' },
        unreadable: { cls: 'unknown',    text: '无法读取', tip: '无法打开文件，检查权限或磁盘' },
        unknown:    { cls: 'unknown',    text: '未知',   tip: '未做校验' },
    };
    const b = map[v] || map.unknown;
    return `<span class="val-badge ${b.cls}" title="${escapeHtml(b.tip)}">${b.text}</span>`;
}

// ⭐ 清理所有 .part 缓存文件
document.getElementById('clear-cache-btn').addEventListener('click', async () => {
    const parts = (localModels || []).filter(m => m.is_part);
    if (!parts.length) {
        toast('没有未完成的缓存文件', 'info');
        return;
    }
    const bytes = parts.reduce((a, b) => a + (b.size || 0), 0);
    if (!confirmDialog(`确定删除 ${parts.length} 个未完成的缓存文件（共 ${formatSize(bytes)}）？`)) return;

    try {
        const res = await fetch(`${API}/api/models/clear-cache`, { method: 'POST' });
        const data = await res.json();
        const n = (data.deleted || []).length;
        toast(n ? `已清理 ${n} 个缓存文件，释放 ${formatSize(data.freed_bytes || 0)}` : '没有可清理的缓存',
              n ? 'success' : 'info');
        await loadLocalModels();
    } catch (e) {
        toast(`清理失败：${e}`, 'error');
    }
});

function statusCellHtml(m) {
    if (!m.has_update_info) {
        return '<span class="upd-badge unknown" title="手动放入的文件没有来源记录">—</span>';
    }
    const cached = updateStatus.get(m.relative_path);
    if (cached) return updateBadgeHtml(cached);

    const last = m.last_check_status;
    if (last) {
        const text = {
            up_to_date: '已是最新', update_available: '有更新',
            local_mismatch: '不完整', check_failed: '检测失败',
        }[last] || last;
        return `<span class="upd-badge ${escapeHtml(last)}">${escapeHtml(text)}</span>`;
    }
    return '<span class="upd-badge idle" title="尚未检测">未检测</span>';
}

function updateBadgeHtml(r) {
    if (!r) return '';
    const text = {
        up_to_date: '已是最新', update_available: '有更新',
        local_mismatch: '不完整', check_failed: '检测失败', unknown: '—',
    }[r.status] || r.status;
    const title = r.message || '';
    return `<span class="upd-badge ${escapeHtml(r.status)}" title="${escapeHtml(title)}">${escapeHtml(text)}</span>`;
}

function bindLocalEvents(container) {
    // 行选择
    container.querySelectorAll('.row-check').forEach(cb => {
        cb.addEventListener('change', () => {
            if (cb.checked) selectedPaths.add(cb.dataset.path);
            else selectedPaths.delete(cb.dataset.path);
            updateSelectionUI();
        });
    });

    // 单文件删除
    container.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirmDialog('确定删除该模型文件？此操作不可恢复。')) return;
            try {
                const res = await fetch(
                    `${API}/api/models?file_path=${encodeURIComponent(btn.dataset.path)}`,
                    { method: 'DELETE' }
                );
                const data = await res.json();
                if (data.error) {
                    toast('删除失败: ' + data.error, 'error');
                    return;
                }
                toast('已删除', 'success');
                loadLocalModels();
            } catch (e) {
                toast('删除请求失败: ' + e.message, 'error');
            }
        });
    });

    // 单行检测
    container.querySelectorAll('.check-one').forEach(btn => {
        btn.addEventListener('click', async () => {
            const rel = btn.dataset.rel;
            btn.disabled = true;
            btn.textContent = '检测中';
            try {
                const res = await fetch(
                    `${API}/api/models/check-one?rel_path=${encodeURIComponent(rel)}`
                );
                const r = await res.json();
                if (!res.ok) {
                    // ⭐ 兜底：接口报错时响应体里没有 message/status，
                    //    直接用它会导致界面显示"xxx：undefined"。
                    throw new Error(r.detail || r.error || `HTTP ${res.status}`);
                }
                updateStatus.set(rel, r);
                applyStatusToRow(container, rel, r);
                // 兜底文案：任何情况下都不显示 undefined
                const text = r.message || r.error || r.status || '检测完成';
                toast(`${rel}：${text}`, r.status === 'check_failed' ? 'error' : 'info');
            } catch (e) {
                toast('检测失败: ' + e.message, 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = '检测';
            }
        });
    });

    // 单行更新
    container.querySelectorAll('.update-one').forEach(btn => {
        btn.addEventListener('click', async () => {
            const rel = btn.dataset.rel;
            btn.disabled = true;
            btn.textContent = '更新中';
            try {
                const res = await fetch(`${API}/api/models/update`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ rel_paths: [rel] }),
                });
                const r = await res.json();
                if (r.started_count) {
                    toast(`已开始更新 ${rel}`, 'success');
                    expandTaskPanel();
                    await refreshTasks();
                } else {
                    toast(r.skipped?.[0]?.reason || '未能启动更新', 'warning');
                }
            } catch (e) {
                toast('更新失败: ' + e.message, 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = '更新';
            }
        });
    });
}

function applyStatusToRow(container, rel, r) {
    const cell = container.querySelector(`.status-cell[data-rel="${cssEscape(rel)}"]`);
    if (cell) cell.innerHTML = updateBadgeHtml(r);
}

function updateSelectionUI() {
    const n = selectedPaths.size;
    const counter = document.getElementById('selected-count');
    if (counter) counter.textContent = `已选 ${n} 个`;

    const all = document.getElementById('select-all');
    const boxes = document.querySelectorAll('.row-check');
    if (all && boxes.length) {
        all.checked = n === boxes.length;
        all.indeterminate = n > 0 && n < boxes.length;
    }

    ['batch-check', 'batch-update', 'batch-delete'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = n === 0;
    });
}

document.getElementById('scan-btn').addEventListener('click', loadLocalModels);

document.getElementById('select-all').addEventListener('change', e => {
    const checked = e.target.checked;
    document.querySelectorAll('.row-check').forEach(cb => {
        cb.checked = checked;
        if (checked) selectedPaths.add(cb.dataset.path);
        else selectedPaths.delete(cb.dataset.path);
    });
    updateSelectionUI();
});

function selectedRelPaths() {
    const rels = [];
    document.querySelectorAll('.row-check').forEach(cb => {
        if (cb.checked && cb.dataset.rel) rels.push(cb.dataset.rel);
    });
    return rels;
}

// 批量检测
document.getElementById('batch-check').addEventListener('click', async () => {
    const rels = selectedRelPaths();
    if (!rels.length) return;
    toast(`正在检测 ${rels.length} 个模型...`, 'info');
    try {
        const res = await fetch(`${API}/api/models/check`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rel_paths: rels }),
        });
        const data = await res.json();
        const container = document.getElementById('local-results');
        (data.results || []).forEach(r => {
            updateStatus.set(r.rel_path, r);
            applyStatusToRow(container, r.rel_path, r);
        });
        const s = data.summary || {};
        toast(`检测完成：${s.up_to_date || 0} 个最新，${s.update_available || 0} 个有更新，`
              + `${s.local_mismatch || 0} 个不完整，${s.check_failed || 0} 个失败`, 'success');
    } catch (e) {
        toast('检测失败: ' + e.message, 'error');
    }
});

// 批量更新
document.getElementById('batch-update').addEventListener('click', async () => {
    const rels = selectedRelPaths();
    if (!rels.length) return;
    if (!confirmDialog(`确定更新选中的 ${rels.length} 个模型？会重新下载并覆盖。`)) return;
    try {
        const res = await fetch(`${API}/api/models/update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rel_paths: rels }),
        });
        const r = await res.json();
        if (r.started_count) {
            toast(`已启动 ${r.started_count} 个更新任务`, 'success');
            expandTaskPanel();
            await refreshTasks();
        }
        if (r.skipped && r.skipped.length) {
            toast(`${r.skipped.length} 个被跳过：${r.skipped[0].reason}`, 'warning');
        }
    } catch (e) {
        toast('更新失败: ' + e.message, 'error');
    }
});

// 批量删除
document.getElementById('batch-delete').addEventListener('click', async () => {
    const paths = Array.from(selectedPaths);
    if (!paths.length) return;
    if (!confirmDialog(`确定删除选中的 ${paths.length} 个模型文件？此操作不可恢复。`)) return;
    try {
        const res = await fetch(`${API}/api/models/batch-delete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_paths: paths }),
        });
        const r = await res.json();
        if (r.deleted_count) toast(`已删除 ${r.deleted_count} 个文件`, 'success');
        if (r.failed && r.failed.length) {
            toast(`${r.failed.length} 个删除失败：${r.failed[0].error}`, 'error');
        }
        loadLocalModels();
    } catch (e) {
        toast('删除失败: ' + e.message, 'error');
    }
});

// 检测全部 / 更新全部
document.getElementById('check-all-btn').addEventListener('click', async () => {
    toast('正在检测所有模型...', 'info');
    try {
        const res = await fetch(`${API}/api/models/check`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rel_paths: [] }),
        });
        const data = await res.json();
        await loadLocalModels();
        (data.results || []).forEach(r => updateStatus.set(r.rel_path, r));
        // 用上次扫描目录重新渲染，把最新检测结果刷进状态列
        renderLocalModels(
            localModels, document.getElementById('local-results'), lastScanDir
        );
        const s = data.summary || {};
        toast(`检测完成：${s.up_to_date || 0} 最新 / ${s.update_available || 0} 有更新 / `
              + `${s.local_mismatch || 0} 不完整 / ${s.check_failed || 0} 失败`, 'success');
    } catch (e) {
        toast('检测失败: ' + e.message, 'error');
    }
});

document.getElementById('update-all-btn').addEventListener('click', async () => {
    const rels = localModels.filter(m => m.has_update_info).map(m => m.relative_path);
    if (!rels.length) {
        toast('没有可更新的模型（需要有来源记录）', 'warning');
        return;
    }
    if (!confirmDialog(`确定更新全部 ${rels.length} 个模型？会重新下载并覆盖。`)) return;
    try {
        const res = await fetch(`${API}/api/models/update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rel_paths: rels }),
        });
        const r = await res.json();
        if (r.started_count) {
            toast(`已启动 ${r.started_count} 个更新任务`, 'success');
            expandTaskPanel();
            await refreshTasks();
        }
    } catch (e) {
        toast('更新失败: ' + e.message, 'error');
    }
});

async function renderScheduleBadge(sched) {
    const el = document.getElementById('schedule-badge');
    if (!el) return;
    if (!sched) {
        try {
            sched = await (await fetch(`${API}/api/models/schedule`)).json();
        } catch (e) {
            el.textContent = '';
            return;
        }
    }
    if (!sched.enabled) {
        el.textContent = '定期检测：未启用';
        el.className = 'schedule-badge off';
    } else {
        el.textContent = `定期检测：每 ${sched.interval_minutes} 分钟`;
        el.className = 'schedule-badge on';
    }
    el.title = sched.last_run_at
        ? `上次检测：${new Date(sched.last_run_at * 1000).toLocaleString()}`
        : '尚未执行过定期检测';
}

// ============================================================
// 设置面板
// ============================================================
const SETTING_FIELDS = {
    // 数据源与日志（Web 面板直接调，不再依赖 docker-compose.yml）
    'set-hf-endpoint': 'hf_endpoint',
    'set-log-buffer': 'log_buffer_size',
    'set-autohide': 'task_panel_autohide',
    'set-scan-path': 'scan_path',
    'set-hf-connect': 'hf_connect_timeout',
    'set-hf-read': 'hf_read_timeout',
    'set-hf-retries': 'hf_max_retries',
    'set-hf-budget': 'hf_search_budget',
    'set-llama-url': 'llamacpp_url',
    'set-llama-key': 'llamacpp_api_key',
    'set-llama-path': 'llamacpp_reload_path',
    'set-llama-cmd': 'llamacpp_reload_cmd',
    'set-update-interval': 'update_check_interval_minutes',
};
// 下拉型设置（不走统一数字/文本解析）
const SETTING_SELECTS = {
    'set-log-level': 'log_level',
    'set-llama-flavor': 'llamacpp_flavor',
};

// 需要从界面值转换回配置值的下拉（默认不转换）
// ⚠️ 之前无脑 toUpperCase()，导致小写的 llamacpp_flavor 回填为空——
//    下拉框选中值丢失，保存时又把空值提交上去。
const SETTING_SELECT_CAST = {
    'set-log-level': v => String(v).toUpperCase(),
    'set-llama-flavor': v => String(v).toLowerCase(),
};

const SETTING_CHECKS = {
    'set-llama-enabled': 'llamacpp_enabled',
    'set-llama-on-download': 'llamacpp_reload_on_download',
    'set-update-enabled': 'update_check_enabled',
};

document.getElementById('settings-btn').addEventListener('click', openSettings);
document.getElementById('close-settings').addEventListener('click', closeSettings);
document.getElementById('settings-cancel').addEventListener('click', closeSettings);

function openSettings() {
    const modal = document.getElementById('settings-modal');
    modal.classList.remove('hidden');
    fillSettingsForm();
}

function closeSettings() {
    document.getElementById('settings-modal').classList.add('hidden');
}

function fillSettingsForm() {
    const v = APP_CONFIG.values || {};
    Object.entries(SETTING_FIELDS).forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (el && v[key] !== undefined) el.value = v[key];
    });
    Object.entries(SETTING_CHECKS).forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (el) el.checked = !!v[key];
    });
    Object.entries(SETTING_SELECTS).forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (!el || v[key] === undefined || v[key] === '') return;
        const cast = SETTING_SELECT_CAST[id];
        el.value = cast ? cast(v[key]) : String(v[key]);
    });
    const mode = v.llamacpp_reload_mode || 'http';
    const sel = document.getElementById('set-llama-mode');
    if (sel) sel.value = mode;
    syncLlamaModeFields(mode);
}

function syncLlamaModeFields(mode) {
    document.querySelectorAll('[data-mode]').forEach(row => {
        row.classList.toggle('hidden', row.dataset.mode !== mode);
    });
}

document.getElementById('set-llama-mode').addEventListener('change', e => {
    syncLlamaModeFields(e.target.value);
});

document.getElementById('settings-save').addEventListener('click', async () => {
    const values = {};
    Object.entries(SETTING_FIELDS).forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (!el || el.value === '') return;
        const num = Number(el.value);
        values[key] = Number.isNaN(num) ? el.value : num;
    });
    Object.entries(SETTING_CHECKS).forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (el) values[key] = el.checked;
    });
    Object.entries(SETTING_SELECTS).forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (el && el.value) values[key] = el.value;
    });
    const modeSel = document.getElementById('set-llama-mode');
    if (modeSel) values.llamacpp_reload_mode = modeSel.value;

    try {
        const res = await fetch(`${API}/api/config`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ values }),
        });
        const data = await res.json();
        if (data.rejected && Object.keys(data.rejected).length) {
            toast(`部分设置未生效：${Object.keys(data.rejected).join('、')}`, 'warning');
        } else {
            toast('设置已保存', 'success');
        }
        // ⭐ 保存后完整重新加载配置并让所有依赖生效：
        //    - 重新拉取配置（页脚版本、级别徽标、扫描路径徽标、消息时长）
        //    - 重画日志（级别/条数变了，视图要跟着变）
        //    - 重画本地列表（扫描路径可能刚改过）
        await loadConfig();
        renderLogLevelBadge();
        renderScanPathBadge();
        updateLogCount();
        renderAllLogs();
        if (document.getElementById('tab-local').classList.contains('active')) {
            await loadLocalModels();
        }
        closeSettings();
    } catch (e) {
        toast('保存失败: ' + e.message, 'error');
    }
});

document.getElementById('settings-reset').addEventListener('click', async () => {
    if (!confirmDialog('确定恢复为环境变量中的默认值？当前 Web 设置将被清除。')) return;
    try {
        await fetch(`${API}/api/config/reset`, { method: 'POST' });
        await loadConfig();
        fillSettingsForm();
        renderLogLevelBadge();
        renderScanPathBadge();
        updateLogCount();
        renderAllLogs();
        toast('已恢复默认值', 'success');
    } catch (e) {
        toast('恢复失败: ' + e.message, 'error');
    }
});

// ⭐ 重新扫描：新增/删除模型后让路由模式重新读盘
document.getElementById('llama-refresh').addEventListener('click', async () => {
    const out = document.getElementById('llama-probe-result');
    out.textContent = '正在重新扫描...';
    try {
        const res = await fetch(`${API}/api/llamacpp/refresh`, { method: 'POST' });
        const r = await safeJson(res);
        if (r.__parseError) { out.textContent = '✕ ' + r.error; return; }
        const models = r.models || [];
        renderLlamaModels(models);
        out.textContent = r.ok
            ? `已重新扫描，共 ${models.length} 个模型`
            : `重新扫描失败：${r.error || '未知错误'}`;
        toast(r.ok ? `已重新扫描，服务端识别到 ${models.length} 个模型` : '重新扫描失败',
              r.ok ? 'success' : 'error');
    } catch (e) {
        out.textContent = '重新扫描失败: ' + e.message;
        toast('重新扫描失败: ' + e.message, 'error');
    }
});

// ⭐ 查看服务端模型列表
document.getElementById('llama-models').addEventListener('click', async () => {
    const out = document.getElementById('llama-probe-result');
    out.textContent = '正在拉取模型列表...';
    try {
        const res = await fetch(`${API}/api/llamacpp/models`);
        const r = await safeJson(res);
        if (r.__parseError) { out.textContent = '✕ ' + r.error; return; }
        const models = r.models || [];
        renderLlamaModels(models);
        out.textContent = r.ok ? `服务端有 ${models.length} 个模型` : `拉取失败：${r.error || '未知'}`;
        if (!r.ok) toast('拉取失败：' + (r.error || '未知'), 'error');
    } catch (e) {
        out.textContent = '拉取失败: ' + e.message;
        toast('拉取模型列表失败: ' + e.message, 'error');
    }
});

/** 渲染服务端模型列表（带状态与"加载"按钮） */
function renderLlamaModels(models) {
    const box = document.getElementById('llama-model-list');
    if (!box) return;
    if (!models.length) {
        box.innerHTML = '<p class="empty">服务端没有返回模型</p>';
        box.classList.remove('hidden');
        return;
    }
    box.innerHTML = `
        <p class="llama-list-title">服务端识别到的模型（${models.length}）</p>
        ${models.map(m => `
            <div class="llama-model-row">
                <span class="llama-model-id">${escapeHtml(m.id)}</span>
                <span class="llama-model-status ${escapeHtml(m.status || '')}">${escapeHtml(m.status || '-')}</span>
                <button class="btn-link llama-load-btn" data-model="${escapeHtml(m.id)}">加载</button>
            </div>
        `).join('')}
    `;
    box.querySelectorAll('.llama-load-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const name = btn.dataset.model;
            btn.disabled = true;
            try {
                const res = await fetch(`${API}/api/llamacpp/reload`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ model_path: `/${name}.gguf` }),
                });
                const r = await res.json();
                toast(r.ok ? `已请求加载 ${name}` : `加载失败：${r.error || '未知'}`,
                      r.ok ? 'success' : 'error');
            } catch (e) {
                toast('加载失败: ' + e.message, 'error');
            } finally {
                btn.disabled = false;
            }
        });
    });
    box.classList.remove('hidden');
}

document.getElementById('llama-probe').addEventListener('click', async () => {
    const el = document.getElementById('llama-probe-result');
    el.textContent = '测试中...';
    try {
        const res = await fetch(`${API}/api/llamacpp/status`);
        const d = await safeJson(res);
        if (d.__parseError) {
            el.textContent = '✕ ' + d.error;
            el.className = 'probe-result bad';
            return;
        }
        const p = d.probe || {};
        el.textContent = p.reachable
            ? `✓ 在线（${p.endpoint || ''} ${p.status_code || ''}）`
            : `✕ ${p.error || '不可达'}`;
        el.className = 'probe-result ' + (p.reachable ? 'ok' : 'bad');
    } catch (e) {
        el.textContent = '✕ ' + e.message;
        el.className = 'probe-result bad';
    }
});

document.getElementById('llama-reload').addEventListener('click', async () => {
    const el = document.getElementById('llama-probe-result');
    el.textContent = '重载中...';
    el.className = 'probe-result';
    try {
        const res = await fetch(`${API}/api/llamacpp/reload`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_path: null }),
        });
        const r = await safeJson(res);
        if (r.__parseError) {
            el.textContent = '✕ ' + r.error;
            el.className = 'probe-result bad';
            return;
        }
        if (r.ok) {
            el.textContent = '✓ 已重载';
            el.className = 'probe-result ok';
            toast('llama.cpp 已重新加载', 'success');
        } else {
            el.textContent = '✕ ' + (r.reason || r.error || '失败');
            el.className = 'probe-result bad';
            toast('重载失败：' + (r.reason || r.error || '未知'), 'error');
        }
    } catch (e) {
        el.textContent = '✕ ' + e.message;
        el.className = 'probe-result bad';
    }
});

// ============================================================
// 日志面板
// ============================================================
let logEntries = [];
let lastLogId = 0;
let logTimer = null;
let logPaused = false;
let bufferStats = null;

const LOG_POLL_INTERVAL = 2000;
const LOG_MAX_ENTRIES = 2000;
const LOG_MAX_DOM_NODES = 1500;

const LEVEL_ORDER = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

function onLogsTabEnter() { startLogPolling(); }
function onLogsTabLeave() { stopLogPolling(); }

function startLogPolling() {
    if (logPaused) { setLogStatus('已暂停', true); return; }
    if (logTimer) return;
    setLogStatus('运行中', false);
    fetchLogs();
    logTimer = setInterval(fetchLogs, LOG_POLL_INTERVAL);
}

function stopLogPolling() {
    if (logTimer) { clearInterval(logTimer); logTimer = null; }
    setLogStatus('已暂停', true);
}

async function fetchLogs() {
    if (!document.getElementById('log-follow').checked) return;

    try {
        const res = await fetch(`${API}/api/logs?since=${lastLogId}&limit=1000`);
        const data = await res.json();
        if (!data || !data.logs || !data.logs.length) {
            updateBufferStats();
            return;
        }

        const fresh = [];
        for (const log of data.logs) {
            if (log.id > lastLogId) {
                lastLogId = log.id;
                fresh.push(log);
            }
        }

        if (fresh.length) {
            logEntries.push(...fresh);
            if (logEntries.length > LOG_MAX_ENTRIES) {
                logEntries = logEntries.slice(-LOG_MAX_ENTRIES);
            }
            appendLogLines(fresh);
        }

        updateLogCount();
        updateBufferStats();
    } catch (e) { /* 静默 */ }
}

async function updateBufferStats() {
    try {
        const res = await fetch(`${API}/api/logs/stats`);
        bufferStats = await res.json();
    } catch (e) { bufferStats = null; }
    updateLogCount();
}

/**
 * 当前最低显示级别。
 *
 * ⭐ 级别选择已从日志页移除，统一由「设置 / 数据源与日志 / 日志级别」决定，
 *    这里直接读配置值，不再有页面下拉（避免两处配置互相打架）。
 */
function currentMinLevel() {
    const name = (APP_CONFIG.values && APP_CONFIG.values.log_level) || 'INFO';
    return LEVEL_ORDER[String(name).toUpperCase()] || 0;
}

/** 在工具栏显示当前生效级别，让"为什么看不到 DEBUG"有处可查 */
function renderLogLevelBadge() {
    const el = document.getElementById('log-level-badge');
    if (!el) return;
    const name = ((APP_CONFIG.values && APP_CONFIG.values.log_level) || 'INFO').toUpperCase();
    el.textContent = `级别 ${name}`;
    el.className = `log-level-badge lv-${name.toLowerCase()}`;
    el.title = `当前生效的日志级别：${name}（在「设置」里修改）`;
}

function currentKeyword() {
    return (document.getElementById('log-search').value || '').trim();
}

function parseKeyword(raw) {
    const m = /^\/(.+)\/([ims]*)$/.exec(raw || '');
    if (m) {
        try {
            return { regex: new RegExp(m[1], m[2] + 'i'), text: null };
        } catch (e) {
            return { regex: null, text: raw };
        }
    }
    return { regex: null, text: (raw || '').toLowerCase() };
}

function passesFilter(log) {
    const minNum = currentMinLevel();
    if (minNum && (LEVEL_ORDER[log.level] || 0) < minNum) return false;

    const kw = parseKeyword(currentKeyword());
    if (kw.regex) return kw.regex.test(`${log.name || ''} ${log.message || ''}`);
    if (kw.text) return `${log.name || ''} ${log.message || ''}`.toLowerCase().includes(kw.text);
    return true;
}

function getFilteredLogs() { return logEntries.filter(passesFilter); }
function logViewEl() { return document.getElementById('log-view'); }
function isAtBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 40; }

function buildLogLine(log) {
    const row = document.createElement('div');
    row.className = `log-line log-${(log.level || '').toLowerCase()}`;
    row.dataset.id = log.id;

    const time = document.createElement('span');
    time.className = 'log-time';
    time.textContent = log.time || '';

    const lvl = document.createElement('span');
    lvl.className = 'log-lvl';
    const badge = document.createElement('span');
    badge.className = 'lvl-badge';
    badge.textContent = log.level || '';
    lvl.appendChild(badge);

    const name = document.createElement('span');
    name.className = 'log-name';
    name.textContent = log.name || '';
    name.title = log.name || '';

    const msg = document.createElement('span');
    msg.className = 'log-msg';
    const highlighted = highlightMatch(log.message || '', currentKeyword());
    if (highlighted !== null) msg.innerHTML = highlighted;
    else msg.textContent = log.message || '';

    row.append(time, lvl, name, msg);
    return row;
}

function appendLogLines(logs) {
    const view = logViewEl();
    const placeholder = view.querySelector('.log-placeholder');
    if (placeholder) placeholder.remove();

    const visible = logs.filter(passesFilter);
    if (!visible.length) return false;

    const wasAtBottom = isAtBottom(view);
    const frag = document.createDocumentFragment();
    for (const log of visible) frag.appendChild(buildLogLine(log));
    view.appendChild(frag);

    trimDomNodes(view);

    if (document.getElementById('log-autoscroll').checked && wasAtBottom) {
        view.scrollTop = view.scrollHeight;
    }
    return true;
}

function renderAllLogs() {
    const view = logViewEl();
    const filtered = getFilteredLogs();

    if (!filtered.length) {
        view.innerHTML = logEntries.length
            ? '<div class="log-placeholder">当前过滤条件下无匹配日志</div>'
            : '<div class="log-placeholder">暂无日志</div>';
        updateLogCount();
        return;
    }

    const start = Math.max(0, filtered.length - LOG_MAX_DOM_NODES);
    const frag = document.createDocumentFragment();
    for (let i = start; i < filtered.length; i++) frag.appendChild(buildLogLine(filtered[i]));
    view.innerHTML = '';
    view.appendChild(frag);

    if (document.getElementById('log-autoscroll').checked) view.scrollTop = view.scrollHeight;
    updateLogCount();
}

function trimDomNodes(view) {
    let overflow = view.childElementCount - LOG_MAX_DOM_NODES;
    while (overflow-- > 0 && view.firstElementChild) view.removeChild(view.firstElementChild);
}

function highlightMatch(text, rawKeyword) {
    if (!rawKeyword) return null;
    const escaped = escapeHtml(text);
    try {
        const { regex, text: plain } = parseKeyword(rawKeyword);
        if (regex) return escaped.replace(new RegExp(regex.source, regex.flags), m => `<mark>${m}</mark>`);
        const safe = plain.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        return escaped.replace(new RegExp(safe, 'gi'), m => `<mark>${m}</mark>`);
    } catch (e) { return null; }
}

function updateLogCount() {
    const el = document.getElementById('log-count');
    if (!el) return;
    const shown = getFilteredLogs().length;
    const buf = bufferStats ? ` · 缓冲 ${bufferStats.size}/${bufferStats.capacity}` : '';
    el.textContent = `${shown} / ${logEntries.length} 条${buf}`;
    el.title = `当前过滤后 ${shown} 条，本地共缓存 ${logEntries.length} 条${buf}`;
}

function setLogStatus(text, paused) {
    const dot = document.querySelector('#log-status .status-dot');
    const label = document.getElementById('log-status-text');
    const box = document.getElementById('log-status');
    if (label) label.textContent = text;
    if (box) box.classList.toggle('paused', !!paused);
    if (dot) dot.classList.toggle('blink', !paused);
}

function logsToText() {
    return getFilteredLogs().map(l => `${l.time} [${l.level}] ${l.name}: ${l.message}`).join('\n');
}

function exportLogs() {
    const text = logsToText();
    if (!text) { toast('当前没有可导出的日志', 'warning'); return; }
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `gguf-manager-${stamp}.log`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

async function copyLogs() {
    const text = logsToText();
    if (!text) { toast('当前没有可复制的日志', 'warning'); return; }
    try {
        await navigator.clipboard.writeText(text);
        setLogStatus('已复制', false);
        setTimeout(() => setLogStatus(logTimer ? '运行中' : '已暂停', !logTimer), 1500);
    } catch (e) {
        toast('浏览器拒绝了剪贴板访问，改为导出文件', 'warning');
        exportLogs();
    }
}

// 级别下拉已移除；设置保存后由 settingsSaved 触发 renderAllLogs
document.getElementById('log-search').addEventListener('input', debounce(() => renderAllLogs(), 200));
document.getElementById('log-search-clear').addEventListener('click', () => {
    document.getElementById('log-search').value = '';
    renderAllLogs();
});
document.getElementById('log-autoscroll').addEventListener('change', () => {
    if (document.getElementById('log-autoscroll').checked) logViewEl().scrollTop = logViewEl().scrollHeight;
});
document.getElementById('log-follow').addEventListener('change', e => {
    if (e.target.checked) { logPaused = false; startLogPolling(); }
    else { stopLogPolling(); setLogStatus('已暂停', true); }
});
document.getElementById('log-refresh').addEventListener('click', () => fetchLogs());
document.getElementById('log-export').addEventListener('click', exportLogs);
document.getElementById('log-copy').addEventListener('click', copyLogs);

document.getElementById('log-clear-view').addEventListener('click', async () => {
    logEntries = [];
    try {
        const res = await fetch(`${API}/api/logs/stats`);
        const stats = await res.json();
        lastLogId = stats.max_id || lastLogId;
    } catch (e) { /* 保持原游标 */ }
    logViewEl().innerHTML = '<div class="log-placeholder">视图已清空，新日志将继续出现</div>';
    updateLogCount();
});

// ============================================================
// 工具函数
// ============================================================
function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function cssEscape(str) {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(str);
    return String(str).replace(/["\\]/g, '\\$&');
}

function formatSize(bytes) {
    if (!bytes) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = bytes, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(2) + ' ' + units[i];
}

function formatSpeed(bps) {
    if (!bps || bps <= 0) return '0 B/s';
    const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
    let v = bps, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? Math.round(v) : v.toFixed(1)) + ' ' + units[i];
}

function formatDuration(sec) {
    if (!sec || sec < 0 || !isFinite(sec)) return '--';
    const s = Math.round(sec);
    if (s < 60) return `${s} 秒`;
    const m = Math.floor(s / 60), rs = s % 60;
    if (m < 60) return `${m} 分 ${rs} 秒`;
    const h = Math.floor(m / 60);
    return `${h} 小时 ${m % 60} 分`;
}

function debounce(fn, wait) {
    let timer = null;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), wait);
    };
}

// ============================================================
// 启动
// ============================================================
(async function boot() {
    await loadConfig();          // 先拿配置（弹窗时长、版本、定期检测状态）
    refreshTasks();              // 恢复可能存在的下载任务面板
})();
