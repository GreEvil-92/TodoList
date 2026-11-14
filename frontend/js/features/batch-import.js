/**
 * 批量导入 - 内嵌简易表格（类 Excel）
 * 依赖：dom-utils.js（Utils）、main.js（App 实例的 reloadAfterDataReplaced / 旧文件导入流程）
 *
 * 设计要点（对标 Excel 的常用操作）：
 *   - 单元格为 contenteditable，双击/点击即编辑；
 *   - 粘贴（Ctrl+V）：Excel/WPS 复制出来的内容是 TSV（\t 分列、\n 分行），
 *     从当前聚焦单元格开始整块填入，行数不足自动追加；首行若为表头（标题/优先级…）自动跳过；
 *   - 复制/剪切（Ctrl+C/X）：框选区域序列化为 TSV 写入剪贴板；
 *   - 框选：按下拖动 / Shift+点击扩展矩形选区，Delete/Backspace 清空选区内容；
 *   - 键盘导航：Tab / Shift+Tab 左右移动，Enter / Shift+Enter 上下移动，
 *     上下方向键移动单元格（在最后一行按 Enter 自动追加新行）；
 *   - 导入：收集非空行 POST 给后端 import_tasks_rows，列序固定为
 *     标题 / 优先级 / 分类 / 截止日期 / 时间，解析规则与 Excel 文件导入完全一致。
 */
(function (global) {
    'use strict';

    // 固定列序：与后端 import_tasks_rows 的默认列映射保持一致
    const COLUMNS = ['title', 'priority', 'category', 'dueDate', 'time'];
    const COLUMN_COUNT = COLUMNS.length;
    const INITIAL_ROWS = 12;
    const MAX_ROWS = 2000;

    // 与后端 _EXCEL_HEADER_ALIASES 对齐：粘贴块首行若是表头则跳过
    const HEADER_ALIASES = ['任务标题', '标题', '任务名称', '优先级', '分类', '类别',
        '截止日期', '截止时间', '截止日', '到期日期', '到期日', '日期', '时间', '时刻'];

    function isHeaderRow(cells) {
        const hit = cells
            .map((c) => String(c || '').trim().replace(/\s+/g, ''))
            .filter((c) => c !== '')
            .filter((c) => HEADER_ALIASES.includes(c));
        // 表头行特征：非空单元格占比高且大多是别名
        return hit.length >= 2 && hit.length === cells.filter((c) => String(c || '').trim()).length;
    }

    function BatchImportManager() {
        this.modal = document.getElementById('batch-import-modal');
        this.gridBody = document.getElementById('batch-grid-body');
        this.gridWrap = document.getElementById('batch-grid-wrap');
        this.confirmBtn = document.getElementById('batch-import-confirm');
        this.clearBtn = document.getElementById('batch-import-clear');
        this.cancelBtn = document.getElementById('batch-import-cancel');
        this.closeBtn = document.getElementById('batch-import-modal-close');
        this.fileBtn = document.getElementById('batch-import-file-btn');

        // 选区模型（Excel 式矩形选区）：anchor 为按下时的起始格，focus 为拖动/Shift 扩展端点
        this._anchor = null;
        this._focus = null;
        this._selecting = false;

        this.bindEvents();
    }

    BatchImportManager.prototype = {

        /* ==================== 弹窗开关 ==================== */

        isOpen() {
            return !!this.modal && this.modal.style.display === 'flex';
        },

        open() {
            if (!this.modal) return;
            this.resetGrid();
            this.modal.classList.remove('is-closing');
            this.modal.classList.add('show');
            this.modal.style.display = 'flex';
        },

        close() {
            if (!this.isOpen()) return;
            Utils.closeModalWithAnimation(this.modal, () => {
                this.modal.classList.remove('show');
                this.modal.style.display = 'none';
            });
        },

        /* ==================== 网格构建 ==================== */

        resetGrid() {
            this.gridBody.innerHTML = '';
            this._anchor = null;
            this._focus = null;
            this._selecting = false;
            for (let i = 0; i < INITIAL_ROWS; i++) this.appendRow();
            // 默认聚焦第一个标题单元格，粘贴即写
            const first = this.cellAt(0, 0);
            if (first) first.focus();
        },

        appendRow() {
            if (this.gridBody.children.length >= MAX_ROWS) return null;
            const tr = document.createElement('tr');
            COLUMNS.forEach(() => {
                const td = document.createElement('td');
                td.className = 'batch-grid__cell';
                td.contentEditable = 'true';
                td.spellcheck = false;
                tr.appendChild(td);
            });
            // 行号
            const indexTd = document.createElement('td');
            indexTd.className = 'batch-grid__index';
            indexTd.textContent = this.gridBody.children.length + 1;
            tr.insertBefore(indexTd, tr.firstChild);
            // 删除行
            const delTd = document.createElement('td');
            delTd.className = 'batch-grid__del';
            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'batch-grid__del-btn';
            delBtn.textContent = '×';
            delBtn.title = '删除此行';
            delBtn.setAttribute('data-i18n-attr', 'title');
            delTd.appendChild(delBtn);
            tr.appendChild(delTd);
            this.gridBody.appendChild(tr);
            this.refreshRowNumbers();
            return tr;
        },

        deleteRow(tr) {
            const rows = this.gridBody.children;
            if (rows.length <= 1) {
                // 仅剩一行时清空内容而不是删掉，保持网格始终可用
                tr.querySelectorAll('.batch-grid__cell').forEach((td) => { td.textContent = ''; });
                return;
            }
            tr.remove();
            this.refreshRowNumbers();
        },

        refreshRowNumbers() {
            Array.prototype.forEach.call(this.gridBody.children, (tr, idx) => {
                const cell = tr.querySelector('.batch-grid__index');
                if (cell) cell.textContent = idx + 1;
            });
        },

        /** 取 (row, col) 位置的单元格；越界返回 null */
        cellAt(row, col) {
            const tr = this.gridBody.children[row];
            if (!tr) return null;
            return tr.querySelectorAll('.batch-grid__cell')[col] || null;
        },

        cellPos(td) {
            const tr = td.parentElement;
            const row = Array.prototype.indexOf.call(this.gridBody.children, tr);
            const col = Array.prototype.indexOf.call(tr.querySelectorAll('.batch-grid__cell'), td);
            return { row, col };
        },

        ensureRows(count) {
            while (this.gridBody.children.length < count) {
                if (!this.appendRow()) break;
            }
        },

        /* ==================== 选区（矩形，Excel 式） ==================== */

        clearSelection() {
            this.gridBody.querySelectorAll('.batch-grid__cell.is-selected')
                .forEach((td) => td.classList.remove('is-selected'));
        },

        renderSelection() {
            this.clearSelection();
            if (!this._anchor || !this._focus) return;
            const r1 = Math.min(this._anchor.row, this._focus.row);
            const r2 = Math.max(this._anchor.row, this._focus.row);
            const c1 = Math.min(this._anchor.col, this._focus.col);
            const c2 = Math.max(this._anchor.col, this._focus.col);
            for (let r = r1; r <= r2; r++) {
                for (let c = c1; c <= c2; c++) {
                    const td = this.cellAt(r, c);
                    if (td) td.classList.add('is-selected');
                }
            }
        },

        selectionRect() {
            if (!this._anchor || !this._focus) return null;
            return {
                r1: Math.min(this._anchor.row, this._focus.row),
                r2: Math.max(this._anchor.row, this._focus.row),
                c1: Math.min(this._anchor.col, this._focus.col),
                c2: Math.max(this._anchor.col, this._focus.col),
            };
        },

        /** 读取选区内容为二维数组（无选区时返回 null） */
        readSelection() {
            const rect = this.selectionRect();
            if (!rect) return null;
            const data = [];
            for (let r = rect.r1; r <= rect.r2; r++) {
                const row = [];
                for (let c = rect.c1; c <= rect.c2; c++) {
                    const td = this.cellAt(r, c);
                    row.push(td ? td.textContent.trim() : '');
                }
                data.push(row);
            }
            return data;
        },

        /** 选中单个单元格（点击时） */
        selectSingle(td) {
            this._anchor = this.cellPos(td);
            this._focus = this._anchor;
            this.renderSelection();
        },

        /* ==================== 粘贴 / 复制 / 剪切 ==================== */

        parseTsv(text) {
            const lines = String(text || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n');
            // 去掉结尾空行（Excel 复制通常带一个结尾换行）
            while (lines.length && lines[lines.length - 1].trim() === '') lines.pop();
            return lines.map((line) => line.split('\t'));
        },

        /** 把二维数据从 (row0, col0) 开始整块写入网格，行数不足自动追加 */
        writeBlock(data, row0, col0) {
            const needRows = row0 + data.length;
            if (needRows > MAX_ROWS) data = data.slice(0, MAX_ROWS - row0);
            this.ensureRows(needRows);
            for (let r = 0; r < data.length; r++) {
                const cells = data[r];
                for (let c = 0; c < cells.length; c++) {
                    const col = col0 + c;
                    if (col >= COLUMN_COUNT) continue; // 固定 5 列，超出丢弃
                    const td = this.cellAt(row0 + r, col);
                    if (td) td.textContent = String(cells[c] == null ? '' : cells[c]);
                }
            }
            // 选中刚粘贴的整块区域（Excel 行为）
            this._anchor = { row: row0, col: col0 };
            this._focus = {
                row: row0 + data.length - 1,
                col: Math.min(col0 + Math.max(...data.map((row) => row.length)) - 1, COLUMN_COUNT - 1),
            };
            this.renderSelection();
        },

        handlePaste(e) {
            const td = e.target.closest('.batch-grid__cell');
            if (!td) return;
            const text = e.clipboardData ? e.clipboardData.getData('text/plain') : null;
            // 剪贴板无文本内容（复制的是文件/图片等）时 getData 返回 ''，直接忽略
            if (!text) return;
            e.preventDefault();

            const block = this.parseTsv(text);
            const multiCell = block.length > 1 || block.some((row) => row.length > 1);
            const pos = this.cellPos(td);

            if (multiCell) {
                // 整块粘贴：跳过表头行
                const data = isHeaderRow(block[0]) ? block.slice(1) : block;
                if (!data.length) return;
                this.writeBlock(data, pos.row, pos.col);
                return;
            }

            // 单格文本：直接写入当前单元格
            td.textContent = block[0][0];
        },

        handleCopy(e, isCut) {
            const data = this.readSelection();
            if (!data) return;
            const rect = this.selectionRect();
            // 单格且非剪切时走浏览器默认（方便往别处粘单值）；其余统一序列化为 TSV
            if (rect.r1 === rect.r2 && rect.c1 === rect.c2 && !isCut) return;
            e.preventDefault();
            const tsv = data.map((row) => row.join('\t')).join('\n');
            e.clipboardData.setData('text/plain', tsv);
            if (isCut) {
                for (let r = rect.r1; r <= rect.r2; r++) {
                    for (let c = rect.c1; c <= rect.c2; c++) {
                        const td = this.cellAt(r, c);
                        if (td) td.textContent = '';
                    }
                }
            }
        },

        /** Delete/Backspace：多格选区时清空选区内容 */
        handleSelectionClear(e) {
            const rect = this.selectionRect();
            if (!rect) return;
            const multi = rect.r1 !== rect.r2 || rect.c1 !== rect.c2;
            const active = document.activeElement;
            const activeIsCell = active && active.classList && active.classList.contains('batch-grid__cell');
            // 聚焦在单格内编辑文本时不拦截，只拦截"框选多格"的清除操作
            if (!multi || !activeIsCell) return;
            e.preventDefault();
            for (let r = rect.r1; r <= rect.r2; r++) {
                for (let c = rect.c1; c <= rect.c2; c++) {
                    const td = this.cellAt(r, c);
                    if (td) td.textContent = '';
                }
            }
        },

        /* ==================== 键盘导航 ==================== */

        focusCell(row, col) {
            const r = Math.max(0, Math.min(row, this.gridBody.children.length - 1));
            const c = Math.max(0, Math.min(col, COLUMN_COUNT - 1));
            const td = this.cellAt(r, c);
            if (!td) return null;
            td.focus();
            this._anchor = { row: r, col: c };
            this._focus = this._anchor;
            this.renderSelection();
            return td;
        },

        /** 光标位于单元格文本边界时方向键才跳出（兼顾单元格内编辑） */
        caretAtEdge(td, direction) {
            const sel = global.getSelection();
            if (!sel || !sel.rangeCount) return true;
            const range = sel.getRangeAt(0);
            if (!range.collapsed) return false;
            const text = td.textContent || '';
            const probe = range.cloneRange();
            probe.selectNodeContents(td);
            if (direction === 'start') { probe.setEnd(range.startContainer, range.startOffset); return probe.toString().length === 0; }
            probe.setStart(range.endContainer, range.endOffset);
            return probe.toString().length === 0;
        },

        handleKeydown(e) {
            const td = e.target.closest ? e.target.closest('.batch-grid__cell') : null;
            if (!td) return;
            const pos = this.cellPos(td);

            if (e.key === 'Tab') {
                e.preventDefault();
                this.focusCell(pos.row, pos.col + (e.shiftKey ? -1 : 1));
                return;
            }
            if (e.key === 'Enter') {
                e.preventDefault();
                const nextRow = pos.row + (e.shiftKey ? -1 : 1);
                // 最后一行向下移动时自动追加新行（Excel 行为）
                if (nextRow >= this.gridBody.children.length && !e.shiftKey) this.appendRow();
                this.focusCell(nextRow, pos.col);
                return;
            }
            if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
                if (td.textContent.trim() === '' || this.caretAtEdge(td, e.key === 'ArrowUp' ? 'start' : 'end')) {
                    e.preventDefault();
                    this.focusCell(pos.row + (e.key === 'ArrowUp' ? -1 : 1), pos.col);
                }
                return;
            }
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                const atEdge = this.caretAtEdge(td, e.key === 'ArrowLeft' ? 'start' : 'end');
                if (td.textContent.trim() === '' || atEdge) {
                    e.preventDefault();
                    this.focusCell(pos.row, pos.col + (e.key === 'ArrowLeft' ? -1 : 1));
                }
                return;
            }
            if (e.key === 'Delete' || e.key === 'Backspace') {
                this.handleSelectionClear(e);
                return;
            }
            if (e.key === 'Escape') {
                td.blur();
            }
        },

        /* ==================== 数据收集与导入 ==================== */

        /** 收集非空数据行（保持用户填写顺序），返回二维数组 */
        collectRows() {
            const rows = [];
            Array.prototype.forEach.call(this.gridBody.children, (tr) => {
                const cells = tr.querySelectorAll('.batch-grid__cell');
                const row = COLUMNS.map((_, i) => (cells[i] ? cells[i].textContent.trim() : ''));
                if (row.some((v) => v !== '')) rows.push(row);
            });
            return rows;
        },

        async importRows() {
            const rows = this.collectRows();
            if (!rows.length) {
                Utils.showToast('表格为空，请先粘贴或填写任务数据', 'warning');
                return;
            }
            Utils.setLoading(true, window.languageManager.getText('batchImporting', '正在导入...'));
            await Utils.apiCall({
                apiMethod: 'import_tasks_rows',
                apiArgs: [rows],
                onSuccess: async (response) => {
                    Utils.setLoading(false);
                    const summary = response.data || {};
                    const imported = summary.imported || 0;
                    const skipped = summary.skipped || 0;
                    let message = window.languageManager.getText('batchImportDone', '批量导入完成');
                    message += '\n' + window.languageManager.getText('importedTasks', '成功导入') + ': ' + imported;
                    if (skipped > 0) {
                        message += '\n' + window.languageManager.getText('skippedTasks', '跳过') + ': ' + skipped;
                    }
                    const createdCats = summary.created_categories || [];
                    if (createdCats.length > 0) {
                        message += '\n' + window.languageManager.getText('createdCategories', '新建分类') + ': ' + createdCats.join('、');
                    }
                    Utils.showToast(message, imported > 0 ? 'success' : 'warning');
                    this.close();
                    this.resetGrid();
                    if (global.App && typeof global.App.reloadAfterDataReplaced === 'function') {
                        await global.App.reloadAfterDataReplaced();
                    }
                },
                onError: (error) => {
                    Utils.setLoading(false);
                    Utils.showToast(
                        window.languageManager.getText('batchImportFailed', '批量导入失败') + ': ' + error.message,
                        'error');
                },
            });
        },

        /* ==================== 事件绑定 ==================== */

        bindEvents() {
            if (!this.modal || !this.gridBody) return;

            // 点击 / 拖动框选（Excel 式矩形选区）
            // mousedown 统一 preventDefault + 手动聚焦：屏蔽原生文本拖选，保证拖动生成的是矩形选区
            this.gridBody.addEventListener('mousedown', (e) => {
                const delBtn = e.target.closest('.batch-grid__del-btn');
                if (delBtn) return; // 删除按钮不参与选区
                const td = e.target.closest('.batch-grid__cell');
                if (!td) return;
                e.preventDefault();
                td.focus();
                this._selecting = true;
                if (e.shiftKey && this._anchor) {
                    this._focus = this.cellPos(td);
                } else {
                    this._anchor = this.cellPos(td);
                    this._focus = this._anchor;
                }
                this.renderSelection();
            });

            this.gridBody.addEventListener('mouseover', (e) => {
                if (!this._selecting) return;
                const td = e.target.closest('.batch-grid__cell');
                if (!td) return;
                this._focus = this.cellPos(td);
                this.renderSelection();
            });

            document.addEventListener('mouseup', () => { this._selecting = false; });

            // 粘贴 / 复制 / 剪切 / 键盘
            this.gridBody.addEventListener('paste', (e) => this.handlePaste(e));
            this.gridBody.addEventListener('copy', (e) => this.handleCopy(e, false));
            this.gridBody.addEventListener('cut', (e) => this.handleCopy(e, true));
            this.gridBody.addEventListener('keydown', (e) => this.handleKeydown(e));

            // 删除行按钮（事件委托）
            this.gridBody.addEventListener('click', (e) => {
                const delBtn = e.target.closest('.batch-grid__del-btn');
                if (!delBtn) return;
                const tr = delBtn.closest('tr');
                if (tr) this.deleteRow(tr);
            });

            this.confirmBtn?.addEventListener('click', () => this.importRows());
            this.clearBtn?.addEventListener('click', () => this.resetGrid());
            this.cancelBtn?.addEventListener('click', () => this.close());
            this.closeBtn?.addEventListener('click', () => this.close());
            this.fileBtn?.addEventListener('click', () => {
                if (global.App && typeof global.App.importTasksFromExcelFile === 'function') {
                    this.close();
                    global.App.importTasksFromExcelFile();
                }
            });
            Utils.bindBackdropClose(this.modal, () => this.close());
        }
    };

    global.BatchImportManager = BatchImportManager;
})(window);
