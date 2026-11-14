# backend/api/mixins/widget_api_mixin.py
"""
桌面显示插件 API Mixin
供主界面侧边栏「桌面显示插件」按钮开关插件小窗。
"""

from typing import Any, Dict

from backend.utils.api_errors import PermissionDeniedError
from backend.utils.response_wrapper import api_handler


class WidgetApiMixin:

    @api_handler
    def toggle_desktop_widget(self) -> Dict[str, Any]:
        """显示/隐藏桌面显示插件小窗，返回切换后的可见状态。

        窗口是进程内单例：首次调用创建（500×500、无边框、置顶），之后只做显示与隐藏，
        因此反复点击不会有 WebView 冷启动开销。
        """
        if getattr(self, 'is_android', False):
            raise PermissionDeniedError('移动端暂不支持桌面显示插件')

        from backend.platforms.impl.desktop.common.desktop_widget import DesktopWidget

        visible = DesktopWidget.get_instance().toggle()
        return {'data': {'visible': visible}}
