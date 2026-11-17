"""上传内容指纹：内容真的没变就不重复上传。

背景：桌面端把数据目录放在 OneDrive 符号链接下时，SQLite 每次打开/关闭、
checkpoint、甚至系统备份都会刷新 ``todo.db`` 的修改时间。增量上传原本只比
「大小 + 修改时间」，于是「什么都没改」也会被判定为变更，每次退出都把整库
重传一遍。

改为再比一层内容哈希：只有哈希变了才算真变更。指纹记录写在数据目录里的
``.sync-state.json``（已在 :func:`is_sync_excluded_file` 中排除出云同步），
所以不会污染云端结构，也不影响其它设备。

哈希只对不超过 ``MAX_HASH_BYTES`` 的文件计算：附件文件名含唯一标识、内容不可变，
本来就不会反复上传，没必要为几百 MB 的历史附件每次算哈希。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Dict, Optional

STATE_FILE_NAME = '.sync-state.json'

# 超过该大小的文件不做内容哈希（当前主库通常 < 5MB）
MAX_HASH_BYTES = 8 * 1024 * 1024

_CHUNK = 1024 * 1024


def is_state_file(relative_name: str) -> bool:
    """是否是本模块的指纹记录文件（或它的临时文件）"""
    base = os.path.basename(str(relative_name).replace('\\', '/'))
    return base == STATE_FILE_NAME or base.startswith(STATE_FILE_NAME + '.')


def fingerprint_of(path: str) -> Optional[Dict[str, Any]]:
    """计算文件的内容指纹 ``{'size': int, 'sha256': str}``。

    文件不存在、超过大小上限或读取失败时返回 None——调用方据此退回
    原来的「大小 + 修改时间」判定，不会因此漏传。
    """
    try:
        size = os.path.getsize(path)
        if size > MAX_HASH_BYTES:
            return None
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return {'size': size, 'sha256': digest.hexdigest()}
    except OSError:
        return None


def load_state(app_dir: str) -> Dict[str, Dict[str, Any]]:
    """读取上次成功上传时的内容指纹；读不到就当作空表"""
    path = os.path.join(app_dir, STATE_FILE_NAME)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        pass
    return {}


def save_state(app_dir: str, state: Dict[str, Dict[str, Any]]) -> None:
    """原子写入指纹表；失败只影响「下次可能多传一次」，绝不抛出打断上传

    内容与磁盘上完全一致时不写盘：数据目录常被 OneDrive 盯着的场景下，
    每次退出都刷新这个文件属于无意义的云上传。
    """
    if state == load_state(app_dir):
        return
    path = os.path.join(app_dir, STATE_FILE_NAME)
    tmp: Optional[str] = None
    try:
        fd, tmp = tempfile.mkstemp(dir=app_dir, prefix=STATE_FILE_NAME + '.',
                                   suffix='.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
