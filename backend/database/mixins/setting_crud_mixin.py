# backend/database/mixins/setting_crud_mixin.py
import json
import sqlite3
from typing import Any, Optional


def query_setting_value(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """读一条设置的原始存储值（纯查询，不管理连接，供常驻只读连接复用）。

    键不存在返回 None；解码（JSON 还原）交给 SettingCrudMixin._decode。
    """
    row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row['value'] if row is not None else None


class SettingCrudMixin:
    """应用设置（键值对，值为 JSON 字符串）"""

    @staticmethod
    def _decode(raw: str) -> Any:
        """尽量把存储值还原为 Python 对象，失败时返回原始字符串。"""
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    @staticmethod
    def _encode(value: Any) -> str:
        """把设置值序列化为存储字符串。"""
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    def get_setting(self, key: str, default_value: Any = None) -> Any:
        """获取单个设置值（查询本体见模块级 query_setting_value）"""
        with self.query() as conn:
            raw = query_setting_value(conn, key)
        if raw is None:
            return default_value
        return self._decode(raw)

    def set_setting(self, key: str, value: Any) -> None:
        """保存单个设置值"""
        with self.tx() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
                (key, self._encode(value))
            )
