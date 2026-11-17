"""
坚果云WebDAV客户端模块
提供连接、目录/文件上传、下载等功能

说明：
- remote_path 现在表示「同步目录」，该目录与本地应用数据目录
  （<存储目录>/todolist，内含 todo.db 与 attachment 附件目录）一一对应。
- 兼容旧版本：若 remote_path 指向的是 db 文件路径，会自动取其父目录作为同步目录。
"""

import os
import re
from pathlib import Path
from typing import Optional, List, Dict, Any
from urllib.parse import urlsplit
from webdav3.client import Client
from webdav3.exceptions import RemoteResourceNotFound
from datetime import datetime, timezone
from backend.utils.logger import LogManager
from backend.utils.api_errors import NotFoundError, ValidationError
from backend.features.webdav.db_sync_safety import (
    SYNC_TMP_SUFFIX, is_sync_excluded_file, safe_replace_live_db)
from backend.features.webdav.sync_fingerprint import (
    fingerprint_of, load_state, save_state)

# 本地应用数据目录中需要同步的数据库文件名
DB_FILE_NAME = 'todo.db'
# 附件目录名
ATTACHMENT_DIR_NAME = 'attachment'


def is_attachment_path(relative_path: str) -> bool:
    """相对应用数据目录的路径是否位于附件目录下

    移动端不同步附件实体文件，但附件的关联信息（名称、路径、链接等）仍随
    todo.db 一起同步，因此这里只需在同步时把 attachment/ 下的文件过滤掉。
    """
    parts = str(relative_path).replace('\\', '/').strip('/').split('/')
    return bool(parts) and parts[0] == ATTACHMENT_DIR_NAME


def _split_webdav_url(url: str) -> tuple:
    """把用户填写的服务器地址拆成 (hostname, root)。

    webdav3 要求 ``webdav_hostname`` 只含「协议 + 域名」，URL 里的路径必须
    放到 ``webdav_root``。如果把带路径的完整地址（坚果云默认
    ``https://dav.jianguoyun.com/dav``）直接塞进 hostname，client 内部的
    ``webdav.root`` 会是空字符串，``list(get_info=True)`` 就无法把
    「被查询目录自身」从结果里过滤掉——坚果云的 PROPFIND 必然返回这一条，
    于是被当成同名子目录继续递归，每轮同步都多打一次无效请求
    （日志里反复出现的 ``/todolist/todolist not found`` 就是它）。

    Returns:
        (hostname, root): hostname 形如 ``https://dav.jianguoyun.com``，
        root 形如 ``/dav``（无路径时为空串）。
    """
    raw = (url or '').strip().rstrip('/')
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw, ''
    return f"{parsed.scheme}://{parsed.netloc}", parsed.path.rstrip('/')


def _normalize_remote_path(path: str) -> str:
    """远程路径归一化：合并重复斜杠、去掉末尾斜杠，根目录返回 '/'"""
    normalized = re.sub(r'/{2,}', '/', str(path or ''))
    stripped = normalized.rstrip('/')
    return stripped or '/'


def _parse_webdav_time(time_str: str) -> float:
    """
    解析WebDAV服务器返回的常见时间格式：
    - GMT格式：Mon, 02 Mar 2026 05:29:30 GMT
    - RFC2822格式：Mon, 02 Mar 2026 05:29:30 +0000（部分服务器/库不写 GMT 字样）
    - ISO格式：2026-03-02T05:29:30Z
    :param time_str: 时间字符串
    :return: 时间戳（float）
    :raises ValueError: 无法识别的格式
    """
    time_str = str(time_str).strip()

    # GMT 后缀：去尾后按无时区解析，显式按 UTC 处理
    if re.search(r'\sGMT$', time_str):
        dt_naive = datetime.strptime(
            re.sub(r'\sGMT$', '', time_str), '%a, %d %b %Y %H:%M:%S')
        return dt_naive.replace(tzinfo=timezone.utc).timestamp()

    # RFC2822（含 GMT / +0000 / -0000 等数字时区）
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(time_str)
        if dt.tzinfo is None:
            # '-0000' 表示未知时区，按 UTC 处理
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        pass

    # ISO 8601（…Z 结尾或带偏移量）
    try:
        return datetime.fromisoformat(time_str.replace('Z', '+00:00')).timestamp()
    except ValueError:
        pass

    raise ValueError(f'无法识别的时间格式: {time_str}')


class WebDAVClient(LogManager):
    """坚果云WebDAV客户端"""

    def __init__(self) -> None:
        super().__init__()
        self.url: Optional[str] = None
        self.client: Optional[Client] = None
        self.username: Optional[str] = None
        self.password: Optional[str] = None
        self.remote_path: Optional[str] = None
        # 服务器根路径（如坚果云的 /dav），用于把「目录自身」条目从清单里剔除
        self.root_path: str = ''

    # ==================== 配置 ====================

    def configure(self, username: str, password: str, remote_path: str,
                  url: str = 'https://dav.jianguoyun.com/dav') -> bool:
        """配置WebDAV连接参数

        Args:
            username: WebDAV用户名
            password: WebDAV密码
            remote_path: 远程同步目录（兼容旧的 db 文件路径，会自动取其父目录）
            url: WebDAV服务器地址
        """
        if not username or not password:
            self.get_logger.error("用户名或密码不能为空")
            return False

        self.username = username
        self.password = password
        self.remote_path = remote_path
        self.url = url

        try:
            hostname, root_path = _split_webdav_url(url)
            self.root_path = root_path
            options = {
                'webdav_hostname': hostname,
                'webdav_login': self.username,
                'webdav_password': self.password,
                # 坚果云对 HEAD 请求返回 403，而 client.check() 正是发 HEAD，
                # 不短路掉的话内部 list() 的存在性校验会直接抛异常。真正的
                # 存在性判断由 _exists()（PROPFIND Depth:0）负责。
                'disable_check': True,
            }
            if root_path:
                options['webdav_root'] = root_path
            self.client = Client(options)
            # 15s 一次轮询都会新建客户端，这里只能是 DEBUG，否则日志被刷屏
            self.get_logger.debug(f"WebDAV客户端已就绪: {hostname}{root_path}")
            return True
        except Exception as e:
            self.get_logger.error(f"WebDAV客户端配置失败: {e}")
            self.client = None
            return False

    @property
    def remote_dir(self) -> str:
        """远程同步目录（标准化为 /a/b 形式，根目录返回 ''）"""
        raw = (self.remote_path or '').strip()
        # 兼容旧版本：如果配置的是 db 文件路径，则取父目录
        if raw.lower().endswith('.db'):
            raw = os.path.dirname(raw)
        raw = raw.strip().strip('/')
        return f'/{raw}' if raw else ''

    def _remote_join(self, *parts: str) -> str:
        """拼接远程路径"""
        segments = [p.strip('/') for p in parts if p and p.strip('/')]
        base = self.remote_dir.strip('/')
        if base:
            segments.insert(0, base)
        return '/' + '/'.join(segments)

    def build_remote_path(self, relative_path: str) -> str:
        """将相对于同步目录的相对路径拼接为完整远程路径"""
        return self._remote_join(relative_path)

    # ==================== 连接测试 ====================

    def test_connection(self) -> None:
        """测试WebDAV连接，并确保同步目录存在"""
        if not self.client:
            raise ValidationError('WebDAV客户端未配置')

        # 列出根目录，验证账号可用
        self.client.list('/')

        # 确保同步目录存在（不存在则创建）
        if self.remote_dir:
            self._ensure_remote_directory(self.remote_dir)

    # ==================== 远程目录 ====================

    def _exists(self, remote_path: str) -> bool:
        """判断远程资源是否存在（PROPFIND Depth:0）。

        不能用 ``client.check()``：它发 HEAD 请求，坚果云对 HEAD 返回 403；
        而本项目为了让 ``list()`` 跑通又必须开着 ``disable_check``，该选项
        会让 ``check()`` 无脑返回 True——两种情况下 ``check()`` 都不可信。
        """
        try:
            self.client.info(remote_path)
            return True
        except RemoteResourceNotFound:
            return False
        except Exception as e:
            self.get_logger.debug(f"探测远程资源失败 {remote_path}: {e}")
            return False

    def _ensure_remote_directory(self, remote_dir: str) -> None:
        """确保远程目录存在，如果不存在则逐级创建"""
        remote_dir = '/' + str(remote_dir).strip('/')
        if remote_dir == '/':
            return

        current = ''
        for part in [p for p in remote_dir.split('/') if p]:
            current += f'/{part}'
            if self._exists(current):
                continue
            try:
                self.client.mkdir(current)
                self.get_logger.debug(f"创建远程目录: {current}")
            except Exception:
                # 目录可能已存在（并发场景），忽略
                pass

    def _remote_file_exists(self, remote_path: str) -> bool:
        """检查远程文件是否存在"""
        try:
            return self._exists(remote_path)
        except Exception as e:
            self.get_logger.error(f"检查远程文件存在性失败: {e}")
            return False

    # ==================== 上传 ====================

    def upload_file(self, remote_file: str, local_file: str,
                    ensured_dirs: Optional[set] = None) -> None:
        """上传单个文件到指定远程路径

        Args:
            ensured_dirs: 本轮已确认存在的远程目录缓存（增量上传批量传文件时
                复用，避免每个文件都向服务器确认一次父目录）；传 None 则每次确认
        """
        if not self.client:
            raise ValidationError('WebDAV客户端未配置')
        if not os.path.exists(local_file):
            raise NotFoundError(f'本地文件不存在: {local_file}')

        parent = os.path.dirname(remote_file)
        if parent and (ensured_dirs is None or parent not in ensured_dirs):
            self._ensure_remote_directory(parent)
            if ensured_dirs is not None:
                ensured_dirs.add(parent)

        self.client.upload_sync(remote_path=remote_file, local_path=local_file)
        self.get_logger.info(f"文件上传成功: {local_file} -> {remote_file}")

    def _remote_full_path(self, remote_dir: str) -> str:
        """远程目录在服务器上的完整路径（含服务器 root，如 /dav/todolist）"""
        root = (self.root_path or '').rstrip('/')
        target = '/' + (remote_dir or '').strip('/')
        return _normalize_remote_path(f"{root}{'' if target == '/' else target}")

    def _collect_remote_info(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """递归收集远程同步目录内文件的修改时间与大小（增量上传比对用）。

        每层目录一次 PROPFIND（get_info 自带 isdir），不再逐个条目发探测
        请求。个别文件时间/大小解析失败时不进入结果——比对不到就当有变化，
        上传兜底，宁可多传不可漏传。

        Returns:
            {相对同步目录的 POSIX 路径: {'modified': UTC时间戳, 'size': 字节数}}；
            同步目录本身在云端不存在时返回 None（首次启用、尚未上传过数据
            的正常状态，调用方据此静默跳过，不能每轮轮询都报一次错）。
        """
        results: Dict[str, Dict[str, Any]] = {}
        visited: set = set()

        def walk(remote_dir: str, prefix: str) -> None:
            current = _normalize_remote_path(remote_dir or '/')
            if current in visited:
                return
            visited.add(current)
            try:
                items = self.client.list(current, get_info=True)
            except RemoteResourceNotFound:
                if not prefix:
                    raise
                # 云端残留的空目录条目：跳过即可，不算同步失败
                self.get_logger.debug(f"远程子目录已不存在，跳过: {current}")
                return
            except Exception:
                if not prefix:
                    raise
                self.get_logger.debug(f"列出远程子目录失败，跳过: {current}")
                return

            # PROPFIND 会把「被查询目录自身」也放进结果：必须按完整路径剔除，
            # 否则它会被当成同名子目录继续递归（请求一个不存在的 /a/a）
            self_path = self._remote_full_path(current)
            for info in items:
                entry_path = _normalize_remote_path(info.get('path') or '')
                if entry_path != '/' and entry_path == self_path:
                    continue
                name = str(info.get('name') or '').strip('/')
                if not name and entry_path != '/':
                    # 少数服务器不返回 displayname，用 href 末段兜底
                    name = entry_path.rsplit('/', 1)[-1]
                if not name:
                    continue
                if info.get('isdir'):
                    walk(f"{current.rstrip('/')}/{name}", f"{prefix}{name}/")
                    continue
                entry: Dict[str, Any] = {'size': -1}
                try:
                    entry['size'] = int(info.get('size') or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    entry['modified'] = _parse_webdav_time(str(info.get('modified')))
                except Exception:
                    pass
                results[f"{prefix}{name}"] = entry

        try:
            walk(self.remote_dir or '', '')
        except RemoteResourceNotFound:
            return None
        return results

    @staticmethod
    def _remote_unchanged(remote_info: Dict[str, Any], local_file: str) -> bool:
        """云端文件与本地是否一致（判定能否跳过上传）。

        服务器在收到上传时会写自己的时间戳（≥本地文件最后修改时间），
        所以「云端时间不旧于本地修改时间（留 1 秒时钟偏差容差）且大小
        一致」即可认为内容相同。时间或大小取不到时不跳过。
        """
        remote_modified = remote_info.get('modified')
        if remote_modified is None or remote_info.get('size', -1) < 0:
            return False
        try:
            local_modified = os.path.getmtime(local_file)
            local_size = os.path.getsize(local_file)
        except OSError:
            return False
        return (remote_info['size'] == local_size
                and remote_modified + 1 >= local_modified)

    def upload_app_dir(self, local_dir: str, include_attachments: bool = True) -> Dict[str, int]:
        """将本地应用数据目录（todo.db + attachment）增量上传到远程同步目录

        两层增量判定：
        1. 云端「大小一致 + 时间不旧」直接跳过（原有逻辑）；
        2. 时间变了但内容哈希与上次成功上传时一致，同样跳过——SQLite 在
           OneDrive 符号链接目录里即使不改数据也会刷新主库修改时间，
           只看时间会导致「什么都没改也每次退出发一遍整库」。

        Args:
            local_dir: 本地应用数据目录
            include_attachments: 是否上传附件实体文件；为 False 时只上传 todo.db
                （附件关联信息仍随数据库同步，仅实体文件不参与同步）

        Returns:
            dict: ``{'uploaded': n, 'unchanged': n, 'skipped': n}``，
            uploaded 为 0 表示云端本来就与本地一致，本次没有任何请求发出。
        """
        local_path = Path(local_dir)
        if not local_path.exists():
            raise NotFoundError(f'本地数据目录不存在: {local_dir}')

        if self.remote_dir:
            self._ensure_remote_directory(self.remote_dir)

        # 先收集本地待上传清单（边车/临时/备份文件与移动端附件在此过滤）
        candidates: List[tuple] = []
        skipped = 0
        for file in local_path.rglob('*'):
            if not file.is_file():
                continue
            rel = file.relative_to(local_path).as_posix()
            if not include_attachments and is_attachment_path(rel):
                skipped += 1
                continue
            if is_sync_excluded_file(rel):
                # WAL 边车（-wal/-shm）与换库临时/备份文件不上云：
                # 边车套上另一个主库会被 SQLite 当合法恢复数据套用，直接写坏库
                skipped += 1
                continue
            candidates.append((rel, str(file)))

        # 云端目录不存在时清单为 None，按「全部待传」处理
        remote_info = self._collect_remote_info() or {}
        ensured_dirs: set = set()
        uploaded = 0
        unchanged = 0
        # 指纹表：记录「上次真正上传的内容哈希」，用于识别时间被刷新但内容未变
        prev_state = load_state(str(local_path))
        new_state: Dict[str, Dict[str, Any]] = {}
        for rel, local_file in candidates:
            info = remote_info.get(rel)
            if info is not None and self._remote_unchanged(info, local_file):
                unchanged += 1
                continue
            fingerprint = fingerprint_of(local_file)
            prev = prev_state.get(rel)
            if (fingerprint is not None and prev is not None
                    and prev.get('sha256') == fingerprint['sha256']
                    and prev.get('size') == fingerprint['size']
                    and info is not None
                    and info.get('size', -1) == fingerprint['size']):
                # 内容与上次上传时完全一致，只是修改时间被刷新了 → 没必要重传
                unchanged += 1
                new_state[rel] = fingerprint
                continue
            self.upload_file(self._remote_join(rel), local_file, ensured_dirs)
            uploaded += 1
            if fingerprint is not None:
                new_state[rel] = fingerprint
        save_state(str(local_path), new_state)

        if uploaded:
            self.get_logger.info(
                f"数据目录上传完成：新传 {uploaded} 个，云端已一致跳过 {unchanged} 个"
                + (f"，跳过附件实体文件 {skipped} 个" if skipped else '')
            )
        else:
            # 无变更属于常态，压到 DEBUG，避免轮询/退出时反复刷屏
            self.get_logger.debug(
                f"数据目录上传完成：无变更（云端已一致 {unchanged} 个"
                + (f"，跳过附件实体文件 {skipped} 个" if skipped else '') + "）"
            )
        return {'uploaded': uploaded, 'unchanged': unchanged, 'skipped': skipped}

    # ==================== 下载 ====================

    def download_file(self, remote_file: str, local_file: str,
                      is_overwrite: bool = False, compare_version: bool = True,
                      compare_against: Optional[str] = None,
                      remote_info: Optional[Dict[str, Any]] = None) -> bool:
        """下载单个远程文件到本地

        Args:
            compare_against: 版本比较参照文件的路径；默认与 local_file 相同。
                数据库走「先下临时文件再换库」时传主库路径，才能用主库的
                修改时间判断远程是否更新。
            remote_info: 目录清单扫描（_collect_remote_info）里该文件的信息。
                提供时跳过存在性检查与单独的 info 请求——整体下载是先扫
                一遍清单再逐个下载，逐文件再探测一次纯属浪费请求。

        Returns:
            bool: 是否实际执行了下载
        """
        if not self.client:
            raise ValidationError('WebDAV客户端未配置')

        if remote_info is None and not self._remote_file_exists(remote_file):
            raise NotFoundError('远程文件不存在')

        local_path = Path(local_file)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if compare_version and not is_overwrite:
            ref_path = compare_against or str(local_path)
            # 参照文件不存在（首次拉取）时没有可比较的版本，直接下载
            if os.path.exists(ref_path):
                try:
                    info = remote_info
                    if info is None or 'modified' not in info:
                        info = self.client.info(remote_file)
                    raw_modified = info['modified']
                    if isinstance(raw_modified, (int, float)):
                        # 目录清单（_collect_remote_info）里已是时间戳，无需再解析
                        remote_modified = float(raw_modified)
                    else:
                        remote_modified = _parse_webdav_time(str(raw_modified))
                    local_modified = os.path.getmtime(ref_path)
                    # 加1秒容差，避免系统时间微小差异导致误判
                    if remote_modified <= local_modified + 1:
                        # 每 15s 轮询都会走到这里，属正常「无新版本」，用 DEBUG
                        self.get_logger.debug("远程文件版本不新于本地，跳过下载")
                        return False
                except Exception as e:
                    # 时间解析失败时直接下载，避免同步中断
                    self.get_logger.warning(f"比较远程/本地文件版本失败，将直接下载: {e}")

        self.client.download_sync(remote_path=remote_file, local_path=local_file)
        return True

    def download_app_dir(self, local_dir: str, is_overwrite: bool = False,
                         include_attachments: bool = True) -> int:
        """从远程同步目录整体下载到本地应用数据目录

        - todo.db：按版本（修改时间）判断，避免覆盖较新的本地数据
        - attachment 附件：文件不可变（文件名含唯一标识），仅下载本地缺失的文件

        Args:
            local_dir: 本地应用数据目录
            is_overwrite: 是否强制用远程数据覆盖本地
            include_attachments: 是否下载附件实体文件；为 False 时只拉取 todo.db
                （附件关联信息仍随数据库同步，仅实体文件不参与同步）

        Returns:
            int: 本次实际下载的文件数，0 表示云端与本地一致
        """
        local_path = Path(local_dir)
        local_path.mkdir(parents=True, exist_ok=True)

        if not self.remote_dir:
            self.get_logger.warning("未配置远程同步目录，跳过下载")
            return 0

        # 一次清单扫描替代逐文件探测：每个文件少发 check/info 各一次请求。
        # 不再先单独 check 目录是否存在——清单扫描本身就能判定，少一次请求。
        remote_infos = self._collect_remote_info()
        if remote_infos is None:
            # 云端还没有这个同步目录：首次启用、数据尚未上传过的正常状态，
            # 15s 一轮的轮询不能每次都记一条 WARNING
            self.get_logger.debug("云端同步目录尚不存在，等待首次上传")
            return 0
        skipped = 0
        downloaded_count = 0

        for rel, rinfo in remote_infos.items():
            if not include_attachments and is_attachment_path(rel):
                skipped += 1
                continue
            if is_sync_excluded_file(rel):
                # 旧版本可能把 todo.db-wal/-shm 传上过云端：这类文件套到
                # 本地主库上会写坏库，一律不落地
                skipped += 1
                continue
            local_file = local_path / rel
            remote_file = self._remote_join(rel)

            if rel == DB_FILE_NAME:
                downloaded = self._download_db_safely(
                    remote_file, str(local_file), is_overwrite, rinfo)
                if downloaded:
                    downloaded_count += 1
                    # 刚同步下来的库就是「云端当前内容」，记下它的指纹，
                    # 否则下次上传会因哈希与上次上传时不同而白传一遍
                    fingerprint = fingerprint_of(str(local_file))
                    if fingerprint is not None:
                        state = load_state(str(local_path))
                        state[rel] = fingerprint
                        save_state(str(local_path), state)
                    # 同步日历提醒
                    try:
                        remote_modified = rinfo.get('modified')
                        if remote_modified is not None:
                            self._get_service().sync_reminder_to_calendar(
                                remote_modified - 1, remote_modified)
                    except Exception:
                        pass
            else:
                if local_file.exists() and not is_overwrite:
                    continue
                local_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.client.download_sync(remote_path=remote_file, local_path=str(local_file))
                    downloaded_count += 1
                except Exception as e:
                    self.get_logger.error(f"下载附件失败 {remote_file}: {e}")

        if downloaded_count:
            self.get_logger.info(
                f"数据目录下载完成，共下载 {downloaded_count} 个文件"
                + (f"，跳过附件实体文件 {skipped} 个" if skipped else '')
            )
        else:
            # 15s 轮询绝大部分时间都是「没有变化」，压到 DEBUG 免得日志被刷满
            self.get_logger.debug(
                "数据目录下载完成：云端无新内容"
                + (f"（跳过附件实体文件 {skipped} 个）" if skipped else '')
            )
        return downloaded_count

    def _get_service(self) -> Any:
        """获取平台服务（日历同步等平台能力）。

        延迟导入：WebDAV 客户端不该在构造时就拉起平台实现；
        取不到时由调用方的 try/except 兜底。
        """
        from backend.platforms.core.factory import get_platform_service
        return get_platform_service()

    def _download_db_safely(self, remote_file: str, live_path: str,
                            is_overwrite: bool,
                            remote_info: Optional[Dict[str, Any]] = None) -> bool:
        """下载主库文件并安全换入正在使用的库。

        先下到 ``todo.db.sync-tmp`` 临时文件（版本比较仍以主库文件为准），
        再交给 :func:`safe_replace_live_db` 完成挂起只读连接、备份、原子
        替换与完整性校验。直接覆盖正在使用的库文件在 Windows 上会失败、
        在 POSIX 上产生旧 WAL 套新库的损坏风险，这里都不能省。

        Returns:
            bool: 远程是否有比本地新的库（即实际执行了换库）
        """
        tmp_path = live_path + SYNC_TMP_SUFFIX
        try:
            downloaded = self.download_file(
                remote_file, tmp_path, is_overwrite,
                compare_against=live_path, remote_info=remote_info)
            if not downloaded:
                return False
            safe_replace_live_db(tmp_path, live_path)
            return True
        finally:
            # 换库成功后 tmp 已不存在（被替换走）；失败/中止时清掉残骸
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
