# -*- coding: utf-8 -*-
"""
===================================
新版 Web 模板层 - HTML 页面生成
===================================

用途：
1. 生成现代化的 WebUI 界面
2. 支持自选股管理（按市场分组）
3. 支持显示完整分析报告
4. 支持股票分析基础功能
"""

from __future__ import annotations

import html
from typing import Optional


# ============================================================
# CSS 样式定义
# ============================================================

MODERN_CSS = """
:root {
    --primary: #3b82f6;
    --primary-dark: #1e40af;
    --primary-light: #dbeafe;
    --success: #10b981;
    --warning: #f59e0b;
    --danger: #ef4444;
    --text: #1f2937;
    --text-light: #6b7280;
    --border: #e5e7eb;
    --bg: #f9fafb;
    --card-bg: #ffffff;
    --hover: #eff6ff;
}

* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
    color: var(--text);
    min-height: 100vh;
    padding: 20px;
}

.page-wrapper {
    max-width: 1400px;
    margin: 0 auto;
}

.navbar {
    background: var(--card-bg);
    border-bottom: 2px solid var(--border);
    padding: 1rem 2rem;
    margin-bottom: 2rem;
    border-radius: 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
}

.navbar h1 {
    font-size: 1.5rem;
    margin: 0;
}

.navbar .stats {
    display: flex;
    gap: 2rem;
    font-size: 0.875rem;
}

.navbar .stat {
    color: var(--text-light);
}

.stat-value {
    font-weight: 600;
    color: var(--primary);
}

.main-content {
    display: grid;
    grid-template-columns: 300px 1fr;
    gap: 2rem;
}

/* 左侧导航 */
.sidebar {
    position: sticky;
    top: 20px;
    height: fit-content;
}

.sidebar-section {
    background: var(--card-bg);
    border-radius: 8px;
    margin-bottom: 1rem;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
}

.sidebar-title {
    background: var(--primary-light);
    padding: 1rem;
    font-weight: 600;
    color: var(--primary-dark);
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

.sidebar-content {
    padding: 1rem;
}

.nav-item {
    display: block;
    padding: 0.75rem 1rem;
    margin-bottom: 0.5rem;
    background: var(--hover);
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s;
    border-left: 3px solid transparent;
}

.nav-item:hover,
.nav-item.active {
    background: var(--primary-light);
    border-left-color: var(--primary);
}

/* 右侧内容区 */
.content {
    display: flex;
    flex-direction: column;
    gap: 2rem;
}

.panel {
    background: var(--card-bg);
    border-radius: 8px;
    padding: 2rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    display: none;
}

.panel.active {
    display: block;
}

.panel-title {
    font-size: 1.25rem;
    font-weight: 600;
    margin-bottom: 1.5rem;
    color: var(--text);
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

/* Watchlist Grid */
.watchlist-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 1.5rem;
    margin-bottom: 2rem;
}

.market-section {
    margin-bottom: 2rem;
}

.market-section h3 {
    font-size: 1.1rem;
    margin-bottom: 1rem;
    color: var(--primary);
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

.stock-card {
    background: linear-gradient(135deg, var(--hover) 0%, var(--card-bg) 100%);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem;
    transition: all 0.3s;
    cursor: pointer;
}

.stock-card.selected {
    border-color: var(--success);
    box-shadow: 0 0 0 3px #d1fae5;
}

.stock-card:hover {
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    transform: translateY(-2px);
    border-color: var(--primary);
}

.stock-card-header {
    display: flex;
    justify-content: space-between;
    align-items: start;
    margin-bottom: 0.75rem;
}

.stock-select {
    margin-right: 0.5rem;
}

.stock-title {
    display: flex;
    align-items: flex-start;
}

.watchlist-toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    align-items: center;
    margin: 1rem 0 1.5rem 0;
}

.watchlist-toolbar .btn-small {
    flex: 0 0 auto;
}

.watchlist-toolbar .summary {
    color: var(--text-light);
    font-size: 0.875rem;
}

.market-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1rem;
}

.stock-code {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--primary);
}

.stock-name {
    font-size: 0.875rem;
    color: var(--text-light);
}

.btn-remove {
    background: var(--danger);
    color: white;
    border: none;
    border-radius: 50%;
    width: 24px;
    height: 24px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s;
    font-size: 0.75rem;
}

.btn-remove:hover {
    background: #dc2626;
}

.stock-actions {
    display: flex;
    gap: 0.5rem;
}

.btn-small {
    flex: 1;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border);
    background: var(--card-bg);
    color: var(--text);
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.875rem;
    transition: all 0.2s;
}

.btn-small:hover {
    background: var(--primary-light);
    border-color: var(--primary);
    color: var(--primary);
}

/* 添加股票表单 */
.add-stock-form {
    background: var(--hover);
    border: 2px dashed var(--border);
    border-radius: 8px;
    padding: 1.5rem;
    margin-bottom: 2rem;
    display: grid;
    grid-template-columns: 1fr 1fr 1fr auto;
    gap: 1rem;
    align-items: end;
}

.form-group {
    display: flex;
    flex-direction: column;
}

label {
    font-size: 0.875rem;
    font-weight: 500;
    margin-bottom: 0.25rem;
    color: var(--text);
}

input, select, textarea {
    padding: 0.75rem;
    border: 1px solid var(--border);
    border-radius: 4px;
    font-size: 0.875rem;
    font-family: inherit;
    transition: all 0.2s;
}

input:focus,
select:focus,
textarea:focus {
    outline: none;
    border-color: var(--primary);
    box-shadow: 0 0 0 3px var(--primary-light);
}

.autocomplete-wrap {
    position: relative;
}

.autocomplete-list {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    background: #fff;
    border: 1px solid var(--border);
    border-radius: 6px;
    box-shadow: 0 8px 16px rgba(0, 0, 0, 0.08);
    max-height: 280px;
    overflow-y: auto;
    z-index: 20;
    display: none;
}

.autocomplete-list.show {
    display: block;
}

.autocomplete-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.6rem 0.75rem;
    cursor: pointer;
    border-bottom: 1px solid #f3f4f6;
}

.autocomplete-item:last-child {
    border-bottom: none;
}

.autocomplete-item:hover {
    background: var(--hover);
}

.autocomplete-meta {
    font-size: 0.75rem;
    color: var(--text-light);
}

button {
    padding: 0.75rem 1.5rem;
    background: var(--primary);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-weight: 500;
    transition: all 0.2s;
}

button:hover {
    background: var(--primary-dark);
}

button:active {
    transform: scale(0.98);
}

button.btn-danger {
    background: var(--danger);
}

button.btn-danger:hover {
    background: #dc2626;
}

/* 分析报告 */
.report-container {
    background: var(--card-bg);
    border-left: 4px solid var(--primary);
    padding: 1.5rem;
    margin-bottom: 1rem;
    border-radius: 4px;
}

.report-header {
    display: flex;
    justify-content: space-between;
    align-items: start;
    margin-bottom: 1rem;
    padding-bottom: 1rem;
    border-bottom: 1px solid var(--border);
}

.report-code {
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--primary);
}

.report-time {
    font-size: 0.75rem;
    color: var(--text-light);
}

.report-summary {
    background: var(--hover);
    padding: 1rem;
    border-radius: 4px;
    margin-bottom: 1rem;
    line-height: 1.6;
}

.report-advice {
    display: inline-block;
    padding: 0.25rem 0.75rem;
    border-radius: 12px;
    font-weight: 600;
    font-size: 0.875rem;
}

.report-advice.buy {
    background: #dcfce7;
    color: #166534;
}

.report-advice.sell {
    background: #fee2e2;
    color: #991b1b;
}

.report-advice.hold {
    background: #fef3c7;
    color: #92400e;
}

.empty-state {
    text-align: center;
    padding: 3rem;
    color: var(--text-light);
}

.empty-state-icon {
    font-size: 3rem;
    margin-bottom: 1rem;
}

/* 响应式 */
@media (max-width: 1024px) {
    .main-content {
        grid-template-columns: 1fr;
    }

    .sidebar {
        position: static;
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 1rem;
    }

    .watchlist-grid {
        grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    }
}

@media (max-width: 768px) {
    .navbar {
        flex-direction: column;
        gap: 1rem;
        text-align: center;
    }

    .navbar .stats {
        flex-direction: column;
        gap: 0.5rem;
    }

    .main-content {
        grid-template-columns: 1fr;
    }

    .sidebar {
        grid-template-columns: 1fr;
    }

    .watchlist-grid {
        grid-template-columns: 1fr;
    }

    .add-stock-form {
        grid-template-columns: 1fr;
    }
}

.loading {
    display: inline-block;
    width: 6px;
    height: 6px;
    background: currentColor;
    border-radius: 50%;
    animation: pulse 1.5s infinite;
}

@keyframes pulse {
    0%, 100% { opacity: 0.3; }
    50% { opacity: 1; }
}

.toast {
    position: fixed;
    bottom: 20px;
    right: 20px;
    background: var(--success);
    color: white;
    padding: 1rem 1.5rem;
    border-radius: 6px;
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    animation: slideIn 0.3s ease-out;
}

.toast.error {
    background: var(--danger);
}

@keyframes slideIn {
    from {
        transform: translateX(400px);
        opacity: 0;
    }
    to {
        transform: translateX(0);
        opacity: 1;
    }
}
"""

# ============================================================
# JavaScript 交互逻辑
# ============================================================

MAIN_JS = """
<script>
class WatchlistUI {
    constructor() {
        this.currentTab = 'watchlistPanel';
        this.markets = ['CN', 'HK', 'US'];
        this.stockData = {};
        this.selectedCodes = new Set();
        this.searchTimer = null;
        this.searchResults = [];
        this.activeSearchField = 'code';
        this.initEventListeners();
        this.loadData();
    }
    
    initEventListeners() {
        // 导航事件
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', (e) => {
                const panelId = e.target.dataset.panel;
                this.switchTab(panelId);
            });
        });
        
        // 添加股票表单
        const addForm = document.getElementById('addStockForm');
        if (addForm) {
            addForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.addStock();
            });
        }

        const selectAllBtn = document.getElementById('selectAllBtn');
        if (selectAllBtn) {
            selectAllBtn.addEventListener('click', () => this.selectAll());
        }

        const clearSelectionBtn = document.getElementById('clearSelectionBtn');
        if (clearSelectionBtn) {
            clearSelectionBtn.addEventListener('click', () => this.clearSelection());
        }

        const analyzeSelectedBtn = document.getElementById('analyzeSelectedBtn');
        if (analyzeSelectedBtn) {
            analyzeSelectedBtn.addEventListener('click', () => this.analyzeSelectedStocks());
        }

        const stockCodeInput = document.getElementById('stockCode');
        const stockNameInput = document.getElementById('stockName');
        const stockMarketSelect = document.getElementById('stockMarket');
        if (stockCodeInput) {
            stockCodeInput.addEventListener('input', (e) => {
                this.activeSearchField = 'code';
                this.handleSearchInput(e.target.value);
            });
            stockCodeInput.addEventListener('focus', (e) => {
                this.activeSearchField = 'code';
                this.handleSearchInput(e.target.value);
            });
        }
        if (stockNameInput) {
            stockNameInput.addEventListener('input', (e) => {
                this.activeSearchField = 'name';
                this.handleSearchInput(e.target.value);
            });
            stockNameInput.addEventListener('focus', (e) => {
                this.activeSearchField = 'name';
                this.handleSearchInput(e.target.value);
            });
        }
        if (stockMarketSelect) {
            stockMarketSelect.addEventListener('change', () => {
                const keyword = document.getElementById('stockCode')?.value || document.getElementById('stockName')?.value || '';
                this.handleSearchInput(keyword);
            });
        }

        document.addEventListener('click', (e) => {
            const boxCode = document.getElementById('stockSuggestList');
            const boxName = document.getElementById('stockNameSuggestList');
            const input = document.getElementById('stockCode');
            const nameInput = document.getElementById('stockName');
            if (!boxCode || !boxName || !input || !nameInput) return;
            if (!boxCode.contains(e.target) && !boxName.contains(e.target) && e.target !== input && e.target !== nameInput) {
                boxCode.classList.remove('show');
                boxName.classList.remove('show');
            }
        });
    }
    
    loadData() {
        fetch('/watchlist/list')
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    this.stockData = data.data || {};
                    this.pruneSelection();
                    this.renderWatchlist(this.stockData);
                    this.updateStats();
                    this.updateSelectionSummary();
                }
            })
            .catch(e => console.error('Failed to load watchlist:', e));
    }
    
    switchTab(panelId) {
        // 隐藏所有 panel
        document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
        document.querySelectorAll('.nav-item').forEach(p => p.classList.remove('active'));
        
        // 显示当前 panel
        const panel = document.getElementById(panelId);
        if (panel) {
            panel.classList.add('active');
            document.querySelector(`[data-panel="${panelId}"]`).classList.add('active');
            this.currentTab = panelId;
        }
    }
    
    renderWatchlist(data) {
        const container = document.getElementById('watchlistContainer');
        if (!container) return;
        
        let html = '';
        
        if (typeof data === 'object' && !Array.isArray(data)) {
            // 分组数据
            for (const [market, stocks] of Object.entries(data)) {
                if (stocks.length === 0) continue;
                
                const marketName = this.getMarketName(market);
                const marketEmoji = this.getMarketEmoji(market);
                
                html += `<div class="market-section">
                    <div class="market-header">
                        <h3>${marketEmoji} ${marketName}</h3>
                        <button class="btn-small" onclick="app.selectByMarket('${market}')">全选${marketName}</button>
                    </div>
                    <div class="watchlist-grid">`;
                
                stocks.forEach(stock => {
                    html += this.renderStockCard(stock);
                });
                
                html += '</div></div>';
            }
        }
        
        if (!html) {
            html = '<div class="empty-state"><div class="empty-state-icon">📌</div><p>还未添加自选股</p></div>';
        }
        
        container.innerHTML = html;
        this.bindCardSelectionEvents();
    }
    
    renderStockCard(stock) {
        const selectedClass = this.selectedCodes.has(stock.code) ? 'selected' : '';
        const checked = this.selectedCodes.has(stock.code) ? 'checked' : '';
        return `
            <div class="stock-card ${selectedClass}" data-code="${stock.code}">
                <div class="stock-card-header">
                    <div class="stock-title">
                        <input type="checkbox" class="stock-select" data-code="${stock.code}" ${checked}>
                        <div>
                            <div class="stock-code">${stock.code}</div>
                            <div class="stock-name">${stock.name || stock.code}</div>
                        </div>
                    </div>
                    <button class="btn-remove" onclick="app.removeStock('${stock.code}')" title="移除">×</button>
                </div>
                <div class="stock-actions">
                    <button class="btn-small" onclick="app.analyzeStock('${stock.code}')">📊 分析</button>
                    <button class="btn-small" onclick="app.viewHistory('${stock.code}')">📜 历史</button>
                </div>
            </div>
        `;
    }

    bindCardSelectionEvents() {
        document.querySelectorAll('.stock-select').forEach(checkbox => {
            checkbox.addEventListener('change', (e) => {
                const code = e.target.dataset.code;
                this.toggleSelection(code, e.target.checked);
            });
        });
    }

    toggleSelection(code, selected) {
        if (!code) return;
        if (selected) {
            this.selectedCodes.add(code);
        } else {
            this.selectedCodes.delete(code);
        }
        const card = document.querySelector(`.stock-card[data-code="${code}"]`);
        if (card) {
            card.classList.toggle('selected', selected);
        }
        this.updateSelectionSummary();
    }

    pruneSelection() {
        const existing = new Set();
        Object.values(this.stockData || {}).forEach(stocks => {
            (stocks || []).forEach(stock => existing.add(stock.code));
        });
        this.selectedCodes = new Set(Array.from(this.selectedCodes).filter(code => existing.has(code)));
    }

    selectAll() {
        Object.values(this.stockData || {}).forEach(stocks => {
            (stocks || []).forEach(stock => this.selectedCodes.add(stock.code));
        });
        this.renderWatchlist(this.stockData);
        this.updateSelectionSummary();
    }

    clearSelection() {
        this.selectedCodes.clear();
        this.renderWatchlist(this.stockData);
        this.updateSelectionSummary();
    }

    selectByMarket(market) {
        const stocks = (this.stockData && this.stockData[market]) || [];
        stocks.forEach(stock => this.selectedCodes.add(stock.code));
        this.renderWatchlist(this.stockData);
        this.updateSelectionSummary();
    }
    
    getMarketName(market) {
        const names = {
            'CN': 'A股',
            'HK': '港股',
            'US': '美股'
        };
        return names[market] || market;
    }
    
    getMarketEmoji(market) {
        const emojis = {
            'CN': '🇨🇳',
            'HK': '🇭🇰',
            'US': '🇺🇸'
        };
        return emojis[market] || '📌';
    }
    
    addStock() {
        const code = document.getElementById('stockCode').value.trim().toUpperCase();
        const name = document.getElementById('stockName').value.trim() || null;
        const market = document.getElementById('stockMarket').value || 'CN';
        
        if (!code) {
            this.showToast('请输入股票代码', 'error');
            return;
        }
        
        fetch(`/watchlist/add?code=${code}&name=${name || ''}&market=${market}`)
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    this.showToast(`✓ 已添加 ${code}`);
                    document.getElementById('addStockForm').reset();
                    this.renderSearchResults([]);
                    this.loadData();
                } else {
                    this.showToast(data.error || '添加失败', 'error');
                }
            })
            .catch(e => this.showToast('请求失败: ' + e.message, 'error'));
    }

    handleSearchInput(keyword) {
        const q = (keyword || '').trim();
        if (!q) {
            this.renderSearchResults([]);
            return;
        }
        if (this.searchTimer) {
            clearTimeout(this.searchTimer);
        }
        this.searchTimer = setTimeout(() => this.fetchSearchResults(q), 180);
    }

    fetchSearchResults(keyword) {
        const market = document.getElementById('stockMarket')?.value || '';
        const query = new URLSearchParams({
            q: keyword,
            market: market,
            limit: '12'
        });
        fetch(`/watchlist/search?${query.toString()}`)
            .then(r => r.json())
            .then(data => {
                this.renderSearchResults((data && data.data) || []);
            })
            .catch(() => this.renderSearchResults([]));
    }

    renderSearchResults(results) {
        this.searchResults = results || [];
        const boxCode = document.getElementById('stockSuggestList');
        const boxName = document.getElementById('stockNameSuggestList');
        if (!boxCode || !boxName) return;
        const box = this.activeSearchField === 'name' ? boxName : boxCode;
        const otherBox = this.activeSearchField === 'name' ? boxCode : boxName;
        otherBox.classList.remove('show');
        otherBox.innerHTML = '';

        if (!this.searchResults.length) {
            box.classList.remove('show');
            box.innerHTML = '';
            return;
        }
        const html = this.searchResults.map(item => {
            const marketName = this.getMarketName(item.market);
            return `
                <div class="autocomplete-item" data-code="${this.escapeHtml(item.code)}" data-name="${this.escapeHtml(item.name || '')}" data-market="${this.escapeHtml(item.market || '')}">
                    <div>
                        <div><strong>${this.escapeHtml(item.code)}</strong> ${this.escapeHtml(item.name || '')}</div>
                        <div class="autocomplete-meta">${this.escapeHtml(marketName)}</div>
                    </div>
                </div>
            `;
        }).join('');
        box.innerHTML = html;
        box.classList.add('show');
        box.querySelectorAll('.autocomplete-item').forEach(el => {
            el.addEventListener('click', () => {
                this.applySuggestion(
                    el.getAttribute('data-code') || '',
                    el.getAttribute('data-name') || '',
                    el.getAttribute('data-market') || ''
                );
            });
        });
    }

    applySuggestion(code, name, market) {
        if (code) {
            document.getElementById('stockCode').value = code;
        }
        if (name) {
            document.getElementById('stockName').value = name;
        }
        if (market && ['CN', 'HK', 'US'].includes(market)) {
            document.getElementById('stockMarket').value = market;
        }
        const boxCode = document.getElementById('stockSuggestList');
        const boxName = document.getElementById('stockNameSuggestList');
        if (boxCode) boxCode.classList.remove('show');
        if (boxName) boxName.classList.remove('show');
    }

    escapeHtml(text) {
        return String(text || '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#39;');
    }
    
    removeStock(code) {
        if (!confirm(`确定要移除 ${code} 吗？`)) return;
        
        fetch(`/watchlist/remove?code=${code}`)
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    this.showToast(`✓ 已移除 ${code}`);
                    this.selectedCodes.delete(code);
                    this.loadData();
                } else {
                    this.showToast('移除失败', 'error');
                }
            })
            .catch(e => this.showToast('请求失败: ' + e.message, 'error'));
    }
    
    analyzeStock(code) {
        const reportType = document.getElementById('batchReportType')?.value || 'simple';
        fetch(`/analysis?code=${encodeURIComponent(code)}&report_type=${encodeURIComponent(reportType)}`)
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    this.showToast(`已提交 ${code} 分析任务`);
                } else {
                    this.showToast(data.error || '提交失败', 'error');
                }
            })
            .catch(e => this.showToast('请求失败: ' + e.message, 'error'));
    }

    analyzeSelectedStocks() {
        const codes = Array.from(this.selectedCodes);
        if (codes.length === 0) {
            this.showToast('请先勾选要分析的股票', 'error');
            return;
        }

        const reportType = document.getElementById('batchReportType')?.value || 'simple';
        const query = new URLSearchParams({
            codes: codes.join(','),
            report_type: reportType
        });

        fetch(`/analysis/batch?${query.toString()}`)
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    this.showToast(`已提交 ${data.submitted_count || 0} 只股票分析任务`);
                } else {
                    this.showToast(data.error || '批量提交失败', 'error');
                }
            })
            .catch(e => this.showToast('请求失败: ' + e.message, 'error'));
    }
    
    viewHistory(code) {
        fetch(`/stock/analysis?code=${code}&limit=5`)
            .then(r => r.json())
            .then(data => {
                if (data.success && data.data.length > 0) {
                    this.showAnalysisHistory(code, data.data);
                } else {
                    this.showToast('暂无分析历史', 'error');
                }
            })
            .catch(e => this.showToast('加载失败', 'error'));
    }
    
    showAnalysisHistory(code, reports) {
        let html = `<h3>📜 ${code} 的分析历史</h3>`;
        reports.forEach(report => {
            html += `
                <div class="report-container">
                    <div class="report-header">
                        <span class="report-code">${report.code}</span>
                        <span class="report-time">${report.created_at ? new Date(report.created_at).toLocaleString('zh-CN') : '-'}</span>
                    </div>
                    <div class="report-summary">
                        ${report.analysis_summary || '暂无摘要'}
                    </div>
                    <div>
                        <span class="report-advice ${report.operation_advice?.includes('买') ? 'buy' : report.operation_advice?.includes('卖') ? 'sell' : 'hold'}">
                            ${report.operation_advice || '待持观'}
                        </span>
                    </div>
                </div>
            `;
        });
        
        const panel = document.getElementById('historyPanel');
        if (panel) {
            panel.innerHTML = html;
            this.switchTab('historyPanel');
        }
    }

    updateStats() {
        const marketCounts = { CN: 0, HK: 0, US: 0 };
        let total = 0;
        Object.entries(this.stockData || {}).forEach(([market, stocks]) => {
            const count = (stocks || []).length;
            total += count;
            if (marketCounts[market] !== undefined) {
                marketCounts[market] = count;
            }
        });

        const totalNode = document.getElementById('totalWatchlistCount');
        if (totalNode) totalNode.textContent = String(total);

        const marketNode = document.getElementById('marketSummary');
        if (marketNode) {
            marketNode.textContent = `A股 ${marketCounts.CN} / 港股 ${marketCounts.HK} / 美股 ${marketCounts.US}`;
        }
    }

    updateSelectionSummary() {
        const count = this.selectedCodes.size;
        const summaryText = `已选 ${count} 只股票`;
        const node1 = document.getElementById('selectedSummary');
        if (node1) node1.textContent = summaryText;
        const node2 = document.getElementById('selectedSummaryAnalyze');
        if (node2) node2.textContent = summaryText;
    }
    
    showToast(message, type = 'success') {
        const toast = document.createElement('div');
        toast.className = 'toast' + (type === 'error' ? ' error' : '');
        toast.textContent = message;
        document.body.appendChild(toast);
        
        setTimeout(() => {
            toast.style.animation = 'slideOut 0.3s ease-in';
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }
}

const app = new WatchlistUI();
</script>
"""


# ============================================================
# 主页面生成函数
# ============================================================

def render_new_main_page() -> bytes:
    """渲染新版主页"""
    
    content = f"""
<div class="page-wrapper">
    <!-- 顶部导航栏 -->
    <div class="navbar">
        <h1>📈 股票分析系统</h1>
        <div class="stats">
            <div class="stat">自选股: <span id="totalWatchlistCount" class="stat-value">0</span></div>
            <div class="stat">市场分布: <span id="marketSummary" class="stat-value">A股 0 / 港股 0 / 美股 0</span></div>
        </div>
    </div>
    
    <!-- 主内容区 -->
    <div class="main-content">
        <!-- 左侧导航 -->
        <div class="sidebar">
            <div class="sidebar-section">
                <div class="sidebar-title">📌 导航</div>
                <div class="sidebar-content">
                    <div class="nav-item active" data-panel="watchlistPanel">📊 自选股</div>
                    <div class="nav-item" data-panel="historyPanel">📜 历史记录</div>
                    <div class="nav-item" data-panel="settingsPanel">⚙️ 设置</div>
                </div>
            </div>
        </div>
        
        <!-- 右侧内容区 -->
        <div class="content">
            <!-- 自选股列表 Panel -->
            <div id="watchlistPanel" class="panel active">
                <div class="panel-title">📌 我的自选股</div>
                
                <!-- 添加股票表单 -->
                <form id="addStockForm" class="add-stock-form">
                    <div class="form-group">
                        <label for="stockCode">股票代码/名称</label>
                        <div class="autocomplete-wrap">
                            <input type="text" id="stockCode" placeholder="输入代码或名称，如 600519 / 茅台 / 腾讯 / AAPL" maxlength="24" autocomplete="off" required>
                            <div id="stockSuggestList" class="autocomplete-list"></div>
                        </div>
                    </div>
                    <div class="form-group">
                        <label for="stockName">股票名称</label>
                        <div class="autocomplete-wrap">
                            <input type="text" id="stockName" placeholder="输入股票名称，如 贵州茅台 / 腾讯控股 / Apple" autocomplete="off">
                            <div id="stockNameSuggestList" class="autocomplete-list"></div>
                        </div>
                    </div>
                    <div class="form-group">
                        <label for="stockMarket">市场</label>
                        <select id="stockMarket">
                            <option value="CN">🇨🇳 A股</option>
                            <option value="HK">🇭🇰 港股</option>
                            <option value="US">🇺🇸 美股</option>
                        </select>
                    </div>
                    <button type="submit">✚ 添加</button>
                </form>

                <div class="watchlist-toolbar">
                    <button id="selectAllBtn" class="btn-small" type="button">全选</button>
                    <button id="clearSelectionBtn" class="btn-small" type="button">清空选择</button>
                    <select id="batchReportType" style="max-width: 140px;">
                        <option value="simple">精简报告</option>
                        <option value="full">完整报告</option>
                    </select>
                    <button id="analyzeSelectedBtn" class="btn-small" type="button">一键分析选中</button>
                    <span id="selectedSummary" class="summary">已选 0 只股票</span>
                </div>
                
                <!-- 自选股列表 -->
                <div id="watchlistContainer">
                    <div class="empty-state">
                        <div class="empty-state-icon">📌</div>
                        <p>加载中...</p>
                    </div>
                </div>
            </div>
            
            <!-- 历史记录 Panel -->
            <div id="historyPanel" class="panel">
                <div class="panel-title">📜 分析历史</div>
                <div class="empty-state">
                    <div class="empty-state-icon">📜</div>
                    <p>点击自选股卡片中的 📜 按钮查看分析历史</p>
                </div>
            </div>
            
            <!-- 设置 Panel -->
            <div id="settingsPanel" class="panel">
                <div class="panel-title">⚙️ 系统设置</div>
                <div style="background: var(--hover); padding: 1rem; border-radius: 4px;">
                    <p>✓ 自选股采用数据库存储，独立于 .env 配置</p>
                    <p>✓ 按市场分组展示（A股、港股、美股）</p>
                    <p>✓ 支持查看历史分析报告</p>
                    <p>✓ API 接口可供外部调用</p>
                </div>
            </div>
        </div>
    </div>
</div>

{MAIN_JS}
"""
    
    html = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>📈 股票分析系统</title>
    <style>
{MODERN_CSS}
    </style>
</head>
<body>
{content}
</body>
</html>
"""
    
    return html.encode("utf-8")
