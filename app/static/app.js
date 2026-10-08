// app/static/app.js
const API = '';

// ========== Tab 切换 ==========
document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
        if (btn.dataset.tab === 'local') loadLocalModels();
    });
});

// ========== 搜索 ==========
document.getElementById('search-btn').addEventListener('click', doSearch);
document.getElementById('search-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') doSearch();
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
            container.innerHTML = `<p class="error">${data.error}</p>`;
            return;
        }
        renderSearchResults(data.results, container);
    } catch (e) {
        container.innerHTML = `<p class="error">搜索失败: ${e.message}</p>`;
    }
}

function renderSearchResults(results, container) {
    if (!results || !results.length) {
        container.innerHTML = '<p class="empty">未找到匹配的模型</p>';
        return;
    }

    container.innerHTML = results.map(r => `
        <div class="model-card">
            <div class="model-info">
                <strong>${escapeHtml(r.id)}</strong>
                <span class="meta">
                    ⬇ ${(r.downloads || 0).toLocaleString()}
                    · ❤ ${r.likes || 0}
                    · ${escapeHtml(r.source)}
                    ${r.has_gguf ? '· <span style="color:#27ae60">GGUF</span>' : ''}
                </span>
            </div>
            <button class="download-btn"
                    data-id="${escapeHtml(r.id)}"
                    data-source="${escapeHtml(r.source)}">
                下载
            </button>
        </div>
    `).join('');

    container.querySelectorAll('.download-btn').forEach(btn => {
        btn.addEventListener('click', () => openDownloadModal(btn.dataset.id, btn.dataset.source));
    });
}

// ========== 下载弹窗 ==========
async function openDownloadModal(modelId, source) {
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
            fileList.innerHTML = `<p class="error">${data.error}</p>`;
            return;
        }

        if (!data.files || !data.files.length) {
            fileList.innerHTML = '<p class="empty">该仓库没有 GGUF 文件</p>';
            return;
        }

        fileList.innerHTML = data.files.map(f => `
            <div class="file-item">
                <span>${escapeHtml(f.filename)}</span>
                <span class="size">${f.size ? formatSize(f.size) : ''}</span>
                <button class="start-download"
                        data-id="${escapeHtml(modelId)}"
                        data-file="${escapeHtml(f.filename)}"
                        data-source="${escapeHtml(source)}">
                    下载
                </button>
            </div>
        `).join('');

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
                        alert('下载启动失败: ' + result.error);
                        btn.disabled = false;
                        btn.textContent = '下载';
                        return;
                    }
                    alert('下载任务已启动，请稍后在"本地管理"页面查看。');
                    modal.classList.add('hidden');
                } catch (e) {
                    alert('请求失败: ' + e.message);
                    btn.disabled = false;
                    btn.textContent = '下载';
                }
            });
        });
    } catch (e) {
        fileList.innerHTML = `<p class="error">加载失败: ${e.message}</p>`;
    }
}

document.getElementById('cancel-download').addEventListener('click', () => {
    document.getElementById('download-modal').classList.add('hidden');
});

document.getElementById('close-modal').addEventListener('click', () => {
    document.getElementById('download-modal').classList.add('hidden');
});

// ========== 本地模型管理 ==========
async function loadLocalModels() {
    const path = document.getElementById('scan-path').value.trim();
    const container = document.getElementById('local-results');
    container.innerHTML = '<p class="loading">扫描中...</p>';

    try {
        const res = await fetch(`${API}/api/models?path=${encodeURIComponent(path)}`);
        const data = await res.json();
        if (data.error) {
            container.innerHTML = `<p class="error">${data.error}</p>`;
            return;
        }
        renderLocalModels(data.models, container, data.scan_dir);
    } catch (e) {
        container.innerHTML = `<p class="error">扫描失败: ${e.message}</p>`;
    }
}

function renderLocalModels(models, container, scanDir) {
    if (!models || !models.length) {
        container.innerHTML = `<p class="empty">在 ${escapeHtml(scanDir || '默认目录')} 未找到本地模型</p>`;
        return;
    }

    container.innerHTML = `
        <p style="margin-bottom:12px;color:#7f8c8d;font-size:13px;">
            扫描目录: ${escapeHtml(scanDir)} · 共 ${models.length} 个模型
        </p>
        <table class="model-table">
            <thead>
                <tr>
                    <th>文件名</th>
                    <th>大小</th>
                    <th>相对路径</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>
                ${models.map(m => `
                    <tr>
                        <td>${escapeHtml(m.filename)}</td>
                        <td>${escapeHtml(m.size_human)}</td>
                        <td class="path-cell">${escapeHtml(m.relative_path)}</td>
                        <td>
                            <button class="delete-btn" data-path="${escapeHtml(m.path)}">删除</button>
                        </td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;

    container.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (!confirm('确定删除该模型文件？此操作不可恢复。')) return;
            try {
                const res = await fetch(
                    `${API}/api/models?file_path=${encodeURIComponent(btn.dataset.path)}`,
                    { method: 'DELETE' }
                );
                const data = await res.json();
                if (data.error) {
                    alert('删除失败: ' + data.error);
                    return;
                }
                loadLocalModels();
            } catch (e) {
                alert('删除请求失败: ' + e.message);
            }
        });
    });
}

document.getElementById('scan-btn').addEventListener('click', loadLocalModels);

// ========== 工具函数 ==========
function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatSize(bytes) {
    if (!bytes) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = bytes;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
    }
    return v.toFixed(2) + ' ' + units[i];
}