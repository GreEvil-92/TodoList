# backend/api/mixins/config_api_mixin.py

from typing import Any, Dict, List, Optional, Union
import backend.globals
from backend.utils import utils
from backend.utils.response_wrapper import api_handler
from backend.utils.api_errors import ValidationError

# 桌面插件的可选项（与 desktop_widget.py 的取值保持一致）
_WIDGET_PRIORITY_KEYS = ('high', 'medium', 'low', 'none')
_WIDGET_RANGE_KEYS = ('today', 'tomorrow', 'week', 'month')


def _normalize_widget_priorities(value: Any) -> List[str]:
    """规整插件优先级范围：只保留合法值、去重保序；允许为空（= 显示无事发生）。"""
    if isinstance(value, str):
        candidates = value.split(',')
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        return ['high']
    seen: List[str] = []
    for item in candidates:
        key = str(item).strip().lower()
        if key in _WIDGET_PRIORITY_KEYS and key not in seen:
            seen.append(key)
    return seen


def _validate_widget_range(value: Any) -> str:
    """规整插件倒计时区间，非法值退回 'month'。"""
    key = str(value).strip().lower()
    return key if key in _WIDGET_RANGE_KEYS else 'month'


class ConfigApiMixin:
    """配置操作 Mixin"""
    CONFIG_REGISTRY = {
        'window_on_top': {
            'key': 'window_on_top',
            'default': False,
            'transform': utils.str_to_bool,
            'post_set': lambda self, value: setattr(
                backend.globals.window, 'on_top',
                utils.str_to_bool(self.db.get_setting('window_on_top', False))
            )
        },
        'shortcut': {
            'key': 'shortcut',
            'default': '<ctrl>+<space>'
        },
        'shortcut_enabled': {
            'key': 'shortcut_enabled',
            'default': True,
            'transform': utils.str_to_bool
        },
        # 主题模式：default（默认）/ dark（深色）/ custom（自定义强调色）
        # 历史值 'light' 由前端 ThemeManager.normalizeMode 兜底为 'default'
        'theme': {
            'key': 'theme',
            'default': 'default'
        },
        'language': {
            'key': 'language',
            'default': 'zh'
        },
        'auto_start': {
            'key': 'auto_start_enabled',
            'default': False,
            'transform': utils.str_to_bool,
            'post_set': lambda self, value: self.service.set_auto_start_system(value)
        },
        # 任务列表可显示的列（前端列的显隐顺序），值为列 key 数组
        'task_list_columns': {
            'key': 'task_list_columns',
            'default': ['name', 'priority', 'dueDate', 'tags']
        },
        # 自定义强调色（深浅各一套），结构见 frontend/js/utils/theme-colors.js
        'custom_accent_colors': {
            'key': 'custom_accent_colors',
            'default': None,
            'validate': utils.normalize_accent_colors
        },
        # 桌面插件显示哪些优先级的任务（多选；空数组 = 范围内无任务，恒显示「无事发生」）
        'widget_priorities': {
            'key': 'widget_priorities',
            'default': ['high'],
            'validate': _normalize_widget_priorities
        },
        # 桌面插件倒计时区间：今日 today / 明日 tomorrow / 本周内 week / 本月内 month
        'widget_countdown_range': {
            'key': 'widget_countdown_range',
            'default': 'month',
            'validate': _validate_widget_range
        }
    }

    @api_handler
    def get_config(self, keys: Optional[Union[str, List[str]]] = None) -> Dict[str, Any]:
        """
        获取配置项
        :param keys: 可选，字符串（逗号分隔）或列表，指定要获取的配置名称；不传则返回全部
        """
        # 确定要遍历的条目
        if keys is None:
            items = self.CONFIG_REGISTRY.items()
        else:
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.split(',') if k.strip()]
            items = [(k, v) for k, v in self.CONFIG_REGISTRY.items() if k in keys]
        result = {}
        for name, cfg in items:
            result[name] = self.db.get_setting(cfg['key'], cfg.get('default'))
            transform = cfg.get('transform')
            if transform:
              result[name] = transform(result[name])
        return result

    @api_handler
    def set_config(self, key: str, value: Any) -> None:
        cfg = self.CONFIG_REGISTRY.get(key)
        if not cfg:
            raise ValidationError(f'未知配置项: {key}')
        # 可选校验钩子：用于在写库前规整/拦截非法值
        validate = cfg.get('validate')
        if validate:
            value = validate(value)
        self.db.set_setting(cfg['key'], value)
        post_set = cfg.get('post_set')
        if post_set:
            result = post_set(self, value) if callable(post_set) else getattr(self, post_set)(value)
            if result is False:
                raise Exception(f"后处理执行失败")