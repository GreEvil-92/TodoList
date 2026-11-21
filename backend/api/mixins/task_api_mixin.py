# backend/api/mixins/task_api_mixin.py
import re
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Union
from backend.features.recurrence import (
    END_COUNT,
    MODE_CRON,
    PREVIEW_OCCURRENCE_LIMIT,
    normalize_rule,
    preview_occurrences,
    validate_rule,
)
from backend.utils.response_wrapper import api_handler
from backend.utils.api_errors import (
    CancelledError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from backend.utils.auto_sync import auto_sync


def _parse_start_date(value: Any) -> Optional[date]:
    """解析周期起始日期（只取日期部分，时间由规则决定）。"""
    if not value:
        return None
    text = str(value).strip()
    if 'T' in text:
        text = text.split('T')[0]
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def validate_due_date(task_data: Union[Dict[str, Any], str]) -> Dict[str, Union[bool, str]]:
    """校验截止时间（完全拷贝原方法）"""
    if isinstance(task_data, dict):
        due_date_str = task_data.get('dueDate')
        if not due_date_str:
            return {'valid': True, 'message': ''}
    else:
        due_date_str = task_data

    if not due_date_str:
        return {'valid': True, 'message': ''}

    try:
        due_date = datetime.fromisoformat(due_date_str)
        # 前端主流路径传本地 naive 串，但 timeline 等处可能带 Z/+08:00 尾，
        # aware 与 naive datetime.now() 比较会抛 TypeError（except ValueError
        # 接不住，直接冒泡成未预期异常）。统一取本地墙上时间处理
        if due_date.tzinfo is not None:
            due_date = due_date.replace(tzinfo=None)
        now = datetime.now()
        if due_date < now:
            return {'valid': False, 'message': '截止时间不能早于当前时间'}
        return {'valid': True, 'message': ''}
    except (ValueError, TypeError):
        return { 'valid': False, 'message': '截止时间格式无效'}


def normalize_due_date(due_date: Optional[str]) -> Union[datetime, Optional[str]]:
    """将截止时间统一解析为时间对象，便于比较；无法解析时返回原值"""
    if not due_date:
        return None
    try:
        return datetime.fromisoformat(due_date)
    except (ValueError, TypeError):
        return due_date


# ==================== Excel 批量导入解析辅助 ==================== #

# 优先级取值 -> 内部枚举（同时兼容中英文与常见别名）
_EXCEL_PRIORITY_MAP = {
    '高': 'high', 'high': 'high', '紧急': 'high',
    '中': 'medium', '普通': 'medium', 'medium': 'medium',
    '低': 'low', 'low': 'low',
    '无': 'none', 'none': 'none', '': 'none',
}

# 内部字段 -> 可识别的表头别名（用户导入模板列名以「任务标题/优先级/分类/截止日期/时间」为准；
# 同时兼容应用自身导出格式「任务名称/任务优先级/任务完成截止时间/任务所属分类」）
_EXCEL_HEADER_ALIASES = {
    'title': ('任务标题', '标题', '任务名称'),
    'priority': ('优先级',),
    'category': ('分类', '类别'),
    'due_date': ('截止日期', '截止时间', '截止日', '到期日期', '到期日', '日期'),
    'time': ('时间', '时刻'),
}

# 分类列中的「无」占位值（导出格式用它表示未设置分类）
_CATEGORY_NONE_SENTINELS = ('无分类', '无')

_DATE_FORMATS = (
    '%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y年%m月%d日',
    '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M',
)

_MIDNIGHT = time(0, 0)


def _match_header(text: Any) -> Optional[str]:
    """把表头单元格文本映射到内部字段名；无法识别返回 None。"""
    if text is None:
        return None
    key = str(text).strip().replace(' ', '').replace('\u3000', '')
    if not key:
        return None
    for field, aliases in _EXCEL_HEADER_ALIASES.items():
        if key in aliases:
            return field
    # 包含匹配兜底：长别名优先，避免「截止日期」被「日期」抢占
    pairs = [(alias, field) for field, alias_list in _EXCEL_HEADER_ALIASES.items() for alias in alias_list]
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    for alias, field in pairs:
        if alias in key:
            return field
    return None


def _map_columns(header_row: Any) -> Dict[str, int]:
    """扫描表头行，返回 {内部字段: 列索引}，同名字段以首次出现为准。"""
    col_map: Dict[str, int] = {}
    for idx, cell in enumerate(header_row or ()):
        field = _match_header(cell)
        if field and field not in col_map:
            col_map[field] = idx
    return col_map


def _normalize_priority(value: Any) -> str:
    """把优先级单元格归一为 high/medium/low/none，无法识别按 none。"""
    if value is None:
        return 'none'
    stripped = str(value).strip()
    return _EXCEL_PRIORITY_MAP.get(stripped, _EXCEL_PRIORITY_MAP.get(stripped.lower(), 'none'))


def _parse_date(value: Any) -> Optional[date]:
    """解析「截止日期」单元格为 date；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        # 常规格式下的 Excel 日期序列号
        try:
            from openpyxl.utils.datetime import from_excel
            converted = from_excel(value)
            if isinstance(converted, datetime):
                return converted.date()
            if isinstance(converted, date):
                return converted
        except Exception:
            return None
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_time(value: Any) -> Optional[str]:
    """解析「时间」单元格为 'HH:MM'；无法解析返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.strftime('%H:%M')
    if isinstance(value, time):
        return value.strftime('%H:%M')
    if isinstance(value, (int, float)):
        # 一天的小数比例（如 0.5 -> 12:00）
        if 0 <= value < 1:
            total_minutes = round(value * 24 * 60)
            return f'{total_minutes // 60:02d}:{total_minutes % 60:02d}'
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.match(r'^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$', text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f'{hour:02d}:{minute:02d}'
    for fmt in ('%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(text, fmt).strftime('%H:%M')
        except ValueError:
            continue
    return None


class TaskApiMixin:
    """任务核心操作 Mixin"""

    @api_handler
    @auto_sync
    def add_todo(self, task_data: Dict[str, Any]) -> Dict[str, Any]:
        """添加新任务"""
        validation_result = validate_due_date(task_data)
        if not validation_result['valid']:
            raise ValidationError(f'{validation_result["message"]}')

        # 用 .get 取值：payload 缺字段时不该在添加任务时抛 KeyError
        due_date_value = task_data.get('dueDate')
        if due_date_value and self.is_android:
            target_time = datetime.fromisoformat(due_date_value).timestamp() * 1000
            self.service.add_task_reminder_to_calendar(
                task_data.get('title'), task_data.get('description'), target_time)
        task = self.db.add_task(task_data)
        # 处理附件（实体文件会被复制到存储目录）
        self._sync_task_attachments(task.get('id'), task_data.get('attachments'))
        return task

    @api_handler
    @auto_sync
    def import_tasks_excel(self) -> Dict[str, Any]:
        """通过 Excel 批量导入任务。

        仅识别以下列（表头名以用户模板为准，兼容常见别名；
        也兼容应用自身「导出任务」生成的 Excel：任务名称/任务优先级/
        任务完成截止时间/任务所属分类，其中「无」占位值会被忽略）：
            任务标题 / 优先级 / 分类 / 截止日期 / 时间

        - 优先级：高/中/低/无（或 high/medium/low/none），无法识别按「无」；
        - 分类：按名称匹配已有分类，找不到则自动新建同名分类；
        - 截止时间：由「截止日期」与「时间」两列合并为 ISO 字符串，缺时间则只取日期；
          「截止时间」列也接受「2026-11-08 21:00」这类日期+时间同格写法；
        - 标题为空的行跳过；批量导入不做「截止时间不早于当前」的校验，便于导入历史任务。

        返回导入结果摘要：{total_rows, imported, skipped, created_categories, errors}。
        """
        if getattr(self, 'is_android', False):
            raise PermissionDeniedError('移动端暂不支持批量导入任务')

        import webview
        from openpyxl import load_workbook

        active_window = webview.active_window()
        if not active_window:
            raise ValidationError('无法获取活动窗口')

        selected = active_window.create_file_dialog(
            webview.FileDialog.OPEN,
            file_types=('Excel Files (*.xlsx)', 'All files (*.*)'),
        )
        if not selected:
            raise CancelledError('用户取消了文件选择')
        file_path = selected[0] if isinstance(selected, (list, tuple)) else selected
        if not file_path:
            raise CancelledError('用户取消了文件选择')

        workbook = load_workbook(filename=str(file_path), data_only=True)
        try:
            worksheet = workbook.active
            rows = list(worksheet.iter_rows(values_only=True))
        finally:
            workbook.close()  # 及时释放文件句柄，避免 Windows 下文件被占用
        if not rows:
            raise ValidationError('Excel 内容为空')

        col_map = _map_columns(rows[0])
        if 'title' not in col_map:
            raise ValidationError('未找到「任务标题」列，请检查表头')

        return self._import_task_rows(rows[1:], col_map, first_line=2)

    @api_handler
    @auto_sync
    def import_tasks_rows(self, rows: Optional[List[List[Any]]] = None) -> Dict[str, Any]:
        """通过前端内嵌表格批量导入任务（「批量导入」弹窗中的简易表格）。

        rows 为二维数组，每行按固定列序：
            [任务标题, 优先级, 分类, 截止日期, 时间]

        单元格取值规则与 import_tasks_excel 完全一致：
        - 优先级：高/中/低/无（或 high/medium/low/none），无法识别按「无」；
        - 分类：按名称匹配已有分类，找不到则自动新建同名分类，「无分类/无」占位值视为未设置；
        - 截止时间：接受「2026-11-08」「2026/11/8」「2026年11月8日」等常见写法；
          「截止日期」单元格内也可写「2026-11-08 21:00」这类日期+时间；
        - 标题为空的行跳过；不校验「截止时间不早于当前」。

        兼容表头行：若首行能按表头别名识别出「任务标题」等列，则按表头映射列，
        从第二行开始导入（用户直接从带表头的 Excel 粘贴也能正确处理）。

        返回导入结果摘要：{total_rows, imported, skipped, created_categories, errors}。
        """
        if getattr(self, 'is_android', False):
            raise PermissionDeniedError('移动端暂不支持批量导入任务')
        if not rows or not isinstance(rows, list):
            raise ValidationError('导入内容为空')

        cleaned: List[List[Any]] = []
        for row in rows:
            if row is None:
                continue
            if isinstance(row, (list, tuple)):
                cleaned.append(list(row))
            else:
                # 单个标量按第一列（任务标题）处理
                cleaned.append([row])

        # 首行若能识别出表头（用户从带表头的 Excel 直接粘贴），按表头映射并跳过首行
        col_map = _map_columns(cleaned[0])
        data_rows = cleaned[1:]
        first_line = 2
        if 'title' not in col_map:
            col_map = {'title': 0, 'priority': 1, 'category': 2, 'due_date': 3, 'time': 4}
            data_rows = cleaned
            first_line = 1

        return self._import_task_rows(data_rows, col_map, first_line=first_line)

    def _import_task_rows(self, rows: List[Any], col_map: Dict[str, int],
                          first_line: int = 1) -> Dict[str, Any]:
        """共享的行级导入逻辑：按 col_map 取列，逐行建任务，返回摘要。"""

        def cell_of(row: Any, field: str) -> Any:
            idx = col_map.get(field)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        # 分类名（小写去空格）-> id 缓存，避免重复查库 / 重复建类
        category_map = {str(c['name']).strip().lower(): c['id'] for c in self.db.get_all_categories()}
        created_categories: List[str] = []

        imported = 0
        skipped = 0
        errors: List[Dict[str, Any]] = []

        for offset, row in enumerate(rows):
            line_no = first_line + offset
            if row is None:
                continue
            title_val = cell_of(row, 'title')
            title = str(title_val).strip() if title_val is not None else ''
            if not title:
                # 整行皆空视为空行，静默跳过；否则记为一条被跳过的数据
                if all(v is None or (isinstance(v, str) and not v.strip()) for v in row):
                    continue
                skipped += 1
                errors.append({'row': line_no, 'reason': '任务标题为空'})
                continue

            task_data: Dict[str, Any] = {
                'title': title,
                'priority': _normalize_priority(cell_of(row, 'priority')),
                'completed': False,
                'categoryId': None,
                'dueDate': None,
            }

            # 分类：命中已有分类则复用，否则新建；导出格式的「无分类/无」占位值视为未设置
            cat_val = cell_of(row, 'category')
            cat_name = str(cat_val).strip() if cat_val is not None else ''
            if cat_name in _CATEGORY_NONE_SENTINELS:
                cat_name = ''
            if cat_name:
                category_id = category_map.get(cat_name.lower())
                if not category_id:
                    new_category = self.db.add_category({'name': cat_name, 'color': '#007bff'})
                    category_id = new_category['id']
                    category_map[cat_name.lower()] = category_id
                    created_categories.append(cat_name)
                task_data['categoryId'] = category_id

            # 截止时间：截止日期 + 时间；日期单元格自带非零点时间且未单列「时间」时取其时间；
            # 兼容导出格式「2026-11-08 21:00」这类日期时间写在同一单元格的写法
            due_date = _parse_date(cell_of(row, 'due_date'))
            time_str = _parse_time(cell_of(row, 'time'))
            raw_due = cell_of(row, 'due_date')
            if due_date and not time_str:
                if isinstance(raw_due, datetime) and raw_due.time() != _MIDNIGHT:
                    time_str = raw_due.strftime('%H:%M')
                elif isinstance(raw_due, str):
                    combined = re.search(r'(\d{1,2}):(\d{2})', raw_due)
                    if combined:
                        hour, minute = int(combined.group(1)), int(combined.group(2))
                        if 0 <= hour <= 23 and 0 <= minute <= 59:
                            time_str = f'{hour:02d}:{minute:02d}'
            if due_date:
                task_data['dueDate'] = f'{due_date.isoformat()}T{time_str}' if time_str else due_date.isoformat()

            try:
                self.db.add_task(task_data)
                imported += 1
            except Exception as exc:  # 单行失败不影响其余行导入
                skipped += 1
                errors.append({'row': line_no, 'reason': str(exc)})

        return {
            'total_rows': len(rows),
            'imported': imported,
            'skipped': skipped,
            'created_categories': created_categories,
            'errors': errors[:50],
        }

    @api_handler
    def get_todos(self, task_filter: Optional[Dict[str, Any]] = None,
                  page: int = 1, page_size: int = 10) -> Dict[str, Any]:
        """分页获取任务，支持多种筛选条件。

        task_filter 为筛选条件对象（见 backend.database.query.TaskFilter），
        可用键（camelCase，未列出的键会被忽略）：
            categoryId / status / priority / dueDateFilter / year / month
            searchQuery / dueDate / dueDateFrom / dueDateTo

        其中 searchQuery 的标签 / 父任务 / 普通文本三种语义由
        backend.database.query 解析层统一处理，多条件之间为 AND：
            结构化（推荐）: {'tags': [{'id','name'}], 'keywords': [...],
                            'parent': {'id','name'}, 'anyTag': True}
            字符串（兼容）: '#标签;关键词' / '>父任务名' / '#'
        """
        return self.db.get_tasks_paginated(task_filter, page=page, page_size=page_size)

    @api_handler
    def get_timeline_tasks(self, task_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """时间轴视图取数（不分页，只回传卡片所需字段）。

        task_filter 与 get_todos 完全同一套（键名与语义见 get_todos 说明），
        区别仅在于：不做分页（时间轴按截止时间区间取数，区间本身就是上限），
        且返回体只有 id / title / dueDate / completed / isRecurring / parentTaskId。
        """
        return self.db.get_timeline_tasks(task_filter)

    @api_handler
    def get_calendar_tasks(self, task_filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """日历视图取数（不分页，只回传月历格所需字段）。

        筛选语义与 get_todos 一致（分类 / 优先级 / 状态 / 搜索 chips），
        由前端把当前生效的筛选条件一起传进来，保证月历与列表口径一致。
        """
        return self.db.get_calendar_tasks(task_filter)

    @api_handler
    def get_task_due_years(self) -> List[str]:
        """导出年份下拉：有截止时间的任务覆盖的年份，已去重、按降序返回。"""
        return self.db.get_task_due_years()

    @api_handler
    def get_task_page(self, task_id: str, task_filter: Optional[Dict[str, Any]] = None,
                      page_size: int = 10) -> Optional[int]:
        """定位任务在当前筛选条件下的页码（从1开始）。

        task_filter 与 get_todos 完全同一套（键名与语义见 get_todos 说明），
        保证「列表看到的分页」与「定位到的分页」永远一致。

        用于快捷键等外部入口新建任务后，把主窗口列表自动翻到新任务所在页；
        任务被当前筛选条件排除时返回 None。
        """
        return self.db.get_task_page(task_id, task_filter, page_size=page_size)

    @api_handler
    def get_todo(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取单个任务"""
        return self.db.get_task(task_id)

    def _refresh_due_date_reminder(self, task_id: str, old_task: Optional[Dict[str, Any]],
                                   new_due_date: Optional[str]) -> None:
        """截止时间发生变化时刷新到期提醒，避免到期后不再提醒

        桌面端：清除已提醒标记，使其在新的截止时间再次弹窗；
        移动端：同步调整系统日历中的提醒时间。
        """
        old_due_date = (old_task or {}).get('dueDate')
        # 统一按时间对象比较，避免「10:30」与「10:30:00」这类格式差异导致误判
        if normalize_due_date(old_due_date) == normalize_due_date(new_due_date):
            return
        try:
            self.service.refresh_task_reminder(task_id, new_due_date, old_due_date)
        except Exception as e:
            self.get_logger.error(f"刷新任务到期提醒失败: {e}")

    @api_handler
    @auto_sync
    def update_todo(self, task_id: str, task_data: Dict[str, Any]) -> Dict[str, Any]:
        """更新任务"""
        old_task = self.db.get_task(task_id)
        # 任务不存在（已被另一端删除/并发删除）时必须报错，而不是让
        # db.update_task 返回 None 被归一成「操作成功」误导前端
        if old_task is None:
            raise NotFoundError(f'任务不存在: {task_id}')
        # 「截止时间不早于当前」只对「本次真的在改截止时间」生效：
        # 前端常回传含原 dueDate 的完整对象，若无条件校验，
        # 已逾期的历史任务连改标题/分类都会被拒绝（导入历史任务是合法的，编辑亦然）
        new_due = task_data.get('dueDate')
        if new_due and new_due != (old_task or {}).get('dueDate'):
            validation_result = validate_due_date(task_data)
            if not validation_result['valid']:
                raise ValidationError(f'{validation_result["message"]}')
        result = self.db.update_task(task_id, task_data)
        # 同步附件（新增/修改/删除）
        self._sync_task_attachments(task_id, task_data.get('attachments'))
        # 截止时间变更后刷新提醒，确保新截止时间到期时能弹窗
        self._refresh_due_date_reminder(task_id, old_task, task_data.get('dueDate'))
        return result

    @api_handler
    @auto_sync
    def delete_todo(self, task_id: str, delete_all: bool = False,
                    delete_scope: Optional[str] = None) -> None:
        """删除任务

        ``delete_scope`` 是周期性任务的删除范围（single / following / all）：
        following 表示「删除本期及后续，但保留其中已完成的任务」。
        不传时按旧的 ``delete_all`` 布尔语义处理，老调用方行为不变。
        """
        # 用户确认删除后，先清理附件记录与实体文件（范围与实际删除的任务一致）
        try:
            for tid in self.db.get_recurring_delete_ids(task_id, delete_scope, delete_all):
                self._cleanup_task_attachments(tid)
        except Exception as e:
            self.get_logger.error(f"清理任务附件失败: {e}")

        self.db.delete_task(task_id, delete_all, delete_scope)

    @api_handler
    @auto_sync
    def add_recurring_todo(self, task_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """添加周期性任务

        周期由 ``recurrenceRule`` 描述（普通模式：每天 / 每周 / 每月 / 每年；Cron 模式）。
        传入的 ``dueDate`` 只取日期部分作为周期起始日，具体提醒时间点由规则决定。
        """
        rule = normalize_rule(task_data.get('recurrenceRule'))

        # 周期起始日期：只需校验日期不早于今天（时间点由规则决定，可能已早于当前时刻）
        start_date = _parse_start_date(task_data.get('dueDate'))
        if start_date is None:
            raise ValidationError('周期性任务必须设置起始日期')
        if start_date < datetime.now().date():
            raise ValidationError('周期起始日期不能早于今天')

        ok, message = validate_rule(rule)
        if not ok:
            raise ValidationError(message)

        # recurrence_type / count 为兼容旧数据与列表展示保留的摘要字段
        task_data['recurrenceType'] = MODE_CRON if rule.get('mode') == MODE_CRON else rule.get('freq')
        task_data['recurrenceCount'] = (
            rule.get('count') if rule.get('endType') == END_COUNT else None)

        result = self.db.create_recurring_tasks(task_data)
        for task in result:
            if task.get('dueDate') and self.is_android:
                target_time = datetime.fromisoformat(task['dueDate']).timestamp() * 1000
                self.service.add_task_reminder_to_calendar(task['title'], task['description'], target_time)
        # 附件挂在周期性任务的父任务上
        if result:
            self._sync_task_attachments(result[0].get('id'), task_data.get('attachments'))
        return result

    @api_handler
    def preview_recurring_occurrences(self, start_date: str, recurrence_rule: Dict[str, Any],
                                      limit: int = PREVIEW_OCCURRENCE_LIMIT) -> List[str]:
        """预览周期性任务接下来会产生的提醒时间（ISO 字符串列表，升序）

        用于在保存前把规则的实际效果展示给用户。
        """
        parsed_start = _parse_start_date(start_date)
        if parsed_start is None:
            raise ValidationError('请先选择周期起始日期')

        rule = normalize_rule(recurrence_rule)
        ok, message = validate_rule(rule)
        if not ok:
            raise ValidationError(message)

        return preview_occurrences(parsed_start, rule, limit=limit)

    @api_handler
    @auto_sync
    def toggle_todo(self, task_id: str) -> Dict[str, Any]:
        """切换任务完成状态

        习惯类周期任务在「标记完成」时按规则续建下一条待办，
        保证同一时刻只有一条未完成实例。
        """
        task = self.db.get_task(task_id)
        if not task:
            raise NotFoundError('Task not found')
        # 只提交变更字段：避免回灌整份任务（含 tags）触发无必要的标签重写
        result = self.db.update_task(task_id, {'completed': not task['completed']})
        # 由未完成 -> 完成时才续建；重新开启不产生新任务
        if not task['completed']:
            self.db.create_next_habit_task(task_id)
        return result

    # 顶部统计条指标已并入 get_task_statistics 的 overview 字段：
    # 两者都基于同一份全量任务计算，拆成两个接口会让顶部条每次刷新都多一次全量加载。