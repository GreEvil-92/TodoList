"""
坚果云WebDAV配置模块
"""

from typing import Dict, Any
from backend.config_manager import get_config_manager

# WebDAV配置常量
WEBDAV_CONFIG_KEY = 'webdav_config'

# 上传频率的可选值：变更即传（默认，改后约1秒）/ 每小时定时上传 / 仅退出时上传
UPLOAD_FREQUENCY_VALUES = ('change', 'hourly', 'on_exit')

def normalize_remote_dir(remote_path: str) -> str:
    """将远程配置标准化为「同步目录」

    - 兼容旧版本：此前 remote_path 指向的是 db 文件路径（如 /todo/todo.db），
      这里自动取其父目录作为同步目录。
    """
    import os
    raw = (remote_path or '').strip()
    if not raw:
        return ''
    if raw.lower().endswith('.db'):
        raw = os.path.dirname(raw)
    return raw.strip().strip('/')


def get_webdav_config() -> Dict[str, Any]:
    """获取WebDAV配置"""
    config = get_config_manager().get(WEBDAV_CONFIG_KEY, {})
    return {
        'enabled': config.get('enabled', False),
        'sync_type': config.get('sync_type', 'jianguoyun'),
        'url': config.get('url', 'https://dav.jianguoyun.com/dav'),
        'username': config.get('username', ''),
        'password': config.get('password', ''),
        'remote_path': normalize_remote_dir(config.get('remote_path', '')),
        'auto_sync': config.get('auto_sync', True),
        'sync_interval': config.get('sync_interval', 15),  # 默认 15s
        'upload_frequency': config.get('upload_frequency', 'change'),  # 默认变更即传
        'first_sync_mode': config.get('first_sync_mode', 'remote_overwrite')  # 默认远程覆盖本地
    }


def set_webdav_config(config: Dict[str, Any]) -> bool:
    """设置WebDAV配置"""
    # 验证配置
    if not isinstance(config, dict):
        raise ValueError("配置必须是字典类型")

    # 验证必要字段
    enabled = config.get('enabled', False)
    if enabled:
        url = config.get('url', '')
        username = config.get('username', '')
        password = config.get('password', '')
        remote_path = normalize_remote_dir(config.get('remote_path', ''))
        if not url or not username or not password or not remote_path:
            raise ValueError("启用WebDAV时，服务器地址、用户名、密码和远程同步目录不能为空")

    # 设置默认值
    webdav_config = {
        'enabled': bool(enabled),
        'sync_type': str(config.get('sync_type', 'jianguoyun')),
        'url': str(config.get('url', 'https://dav.jianguoyun.com/dav')),
        'username': str(config.get('username', '')),
        'password': str(config.get('password', '')),
        'remote_path': normalize_remote_dir(str(config.get('remote_path', ''))),
        'auto_sync': bool(config.get('auto_sync', True)),
        # 下限 5 秒：更小的间隔配合自续定时器会打爆 WebDAV 服务器
        'sync_interval': max(5, int(config.get('sync_interval', 15))),
        'upload_frequency': str(config.get('upload_frequency', 'change') or 'change'),
        'first_sync_mode': str(config.get('first_sync_mode', 'remote_overwrite'))  # local_overwrite | remote_overwrite
    }
    # 非法值兜底退回默认，避免脏配置让上传链路静默失效
    if webdav_config['upload_frequency'] not in UPLOAD_FREQUENCY_VALUES:
        webdav_config['upload_frequency'] = 'change'

    return get_config_manager().set(WEBDAV_CONFIG_KEY, webdav_config)


def is_webdav_enabled() -> bool:
    """检查WebDAV是否启用"""
    config = get_webdav_config()
    return config.get('enabled', False) and config.get('username') and config.get('password') and config.get(
        'remote_path')