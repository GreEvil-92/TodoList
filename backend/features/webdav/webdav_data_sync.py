"""
数据同步管理模块
负责处理WebDAV数据同步逻辑
"""

import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple
from datetime import datetime
from backend.features.webdav.webdav_config import get_webdav_config, is_webdav_enabled, set_webdav_config
from backend.features.webdav.webdav_client import WebDAVClient
from backend.features.webdav.db_sync_safety import checkpoint_wal
from backend.platforms.core.factory import is_mobile_platform
from backend.utils.logger import LogManager
from backend.utils.api_errors import (
    ConflictError, DatabaseError, NotFoundError, ValidationError)

class DataSyncManager(LogManager):
    """数据同步管理器"""
    
    def __init__(self) -> None:
        super().__init__()
        self.sync_timer: Optional[threading.Timer] = None
        self.upload_timer: Optional[threading.Timer] = None
        # 同步链代际：start/stop 都会递增。定时链的 wrapper 在 finally 里自续，
        # 若只靠 cancel() 停止，正在执行中的 wrapper 仍会续排新定时器（停止失效/
        # 重启后双链并发），故续排前必须校对代际号
        self._sync_gen: int = 0
        # 互斥锁：is_syncing 只是给外部看的状态标记，真正的「同一时刻只允许
        # 一次同步」靠非阻塞 acquire 实现——标记的检查和置位不是原子的，
        # 15s 轮询和用户手动同步同时进来时两者都可能通过检查
        self._sync_op_lock = threading.Lock()
        # 变更上传的防抖定时器：连续编辑时每次变更都重置 1s 窗口，
        # 只在停手后真正上传一次，不再每笔变更各起一个线程各传一轮
        self._debounce_lock = threading.Lock()
        self._debounce_timer: Optional[threading.Timer] = None
        self.is_syncing = False
        self.last_sync_time: Optional[datetime] = None
        self.on_sync_callback: Optional[Callable[[], None]] = None
        # 退出上传只允许真正执行一次：托盘「彻底退出」和 start.py 的 finally
        # 是两条独立的退出路径，都会调 upload_on_exit，用事件标记保证幂等
        self._exit_upload_done = threading.Event()
        # 同步失败日志去重：断网 / 服务器异常时每 15s 一条 ERROR 同样会把日志
        # 刷满，改为「同一错误 5 分钟内只报一次」，恢复后补一条 INFO
        self._failure_lock = threading.Lock()
        self._last_failure: Optional[Tuple[str, float]] = None

    def set_sync_callback(self, callback: Callable[[], None]) -> None:
        """设置同步回调函数"""
        self.on_sync_callback = callback

    @property
    def syncs_attachment_files(self) -> bool:
        """是否同步附件实体文件

        移动端不支持附件实体文件同步（无本地文件能力、且避免大文件占空间），
        但附件的关联信息仍随 todo.db 一起同步。
        """
        return not is_mobile_platform()
    
    def start_auto_sync(self) -> None:
        """启动自动同步"""
        config = get_webdav_config()
        
        if not config.get('enabled', False) or not config.get('auto_sync', True):
            self.get_logger.info("自动同步未启用")
            return
            
        # 下限保护：0/负数间隔会让自续定时器变成死循环，打爆 WebDAV 服务器
        interval = max(5, int(config.get('sync_interval', 15)))  # 默认15s

        # 取消之前的定时器，并递增代际使任何在途 wrapper 的自续失效
        self._sync_gen += 1
        gen = self._sync_gen
        if self.sync_timer:
            self.sync_timer.cancel()
        if self.upload_timer:
            self.upload_timer.cancel()
            self.upload_timer = None

        # 启动定时同步（云端拉取不受上传频率影响，始终按 sync_interval 轮询）
        self._schedule_sync(interval, gen)
        self.get_logger.info(f"自动同步已启动，间隔: {interval}秒")

        # 上传频率为「每小时」时，另起一条固定间隔的上传定时器；
        # 取应用内相对间隔（从启动/上次上传起算），不追系统整点——
        # 整点对齐要处理时区、睡眠唤醒后补传等问题，相对间隔更简单不易出错
        if config.get('upload_frequency', 'change') == 'hourly':
            self._schedule_upload(3600, gen)
            self.get_logger.info("定时上传已启动，间隔: 3600秒")

    def stop_auto_sync(self) -> None:
        """停止自动同步"""
        # 先递增代际，让正在执行中的 wrapper 在 finally 里不再续排
        self._sync_gen += 1
        if self.sync_timer:
            self.sync_timer.cancel()
            self.sync_timer = None
            self.get_logger.info("自动同步已停止")
        if self.upload_timer:
            self.upload_timer.cancel()
            self.upload_timer = None
            self.get_logger.info("定时上传已停止")
        with self._debounce_lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None

    def _schedule_upload(self, interval: int, gen: int) -> None:
        """安排下次定时上传"""
        def upload_wrapper() -> None:
            try:
                self.sync_to_cloud()
                self._report_sync_success()
            except Exception as e:
                self._report_sync_failure("定时上传", e)
            finally:
                # 仅当代际未变（未被 stop/重启作废）才续排下一次上传
                if gen == self._sync_gen:
                    self._schedule_upload(interval, gen)

        self.upload_timer = threading.Timer(interval, upload_wrapper)
        self.upload_timer.daemon = True
        self.upload_timer.start()
    
    def _schedule_sync(self, interval: int, gen: int) -> None:
        """安排下次同步"""
        def sync_wrapper() -> None:
            try:
                self.sync_from_cloud()
                self._report_sync_success()
                # 轮询本身不算「事件」：有无变化已由 sync_from_cloud 按级别记录，
                # 这里再补一条 INFO 就是每 15s 固定刷一行
                self.get_logger.debug("定时同步已完成")
            except Exception as e:
                self._report_sync_failure("定时同步", e)
            finally:
                # 仅当代际未变（未被 stop/重启作废）才续排下一次同步
                if gen == self._sync_gen:
                    self._schedule_sync(interval, gen)

        self.sync_timer = threading.Timer(interval, sync_wrapper)
        self.sync_timer.daemon = True
        self.sync_timer.start()

    # 同一失败信息的折叠窗口（秒）：断网时每轮轮询都会抛同样的异常，
    # 5 分钟内只保留第一条 ERROR，其余降到 DEBUG
    FAILURE_LOG_WINDOW = 300

    def _report_sync_failure(self, action: str, error: Exception) -> None:
        """记录同步失败，相同错误在窗口期内只报一次"""
        message = f"{action}出错: {error}"
        now = time.time()
        with self._failure_lock:
            previous = self._last_failure
            self._last_failure = (message, now)
        if previous and previous[0] == message and now - previous[1] < self.FAILURE_LOG_WINDOW:
            self.get_logger.debug(f"{message}（{self.FAILURE_LOG_WINDOW} 秒内重复，已折叠）")
            return
        self.get_logger.error(message)

    def _report_sync_success(self) -> None:
        """同步恢复正常时补一条 INFO，避免「出错后无声恢复」"""
        with self._failure_lock:
            had_failure = self._last_failure is not None
            self._last_failure = None
        if had_failure:
            self.get_logger.info("数据同步已恢复正常")

    def _new_client(self) -> WebDAVClient:
        """按当前配置新建一个 WebDAV 客户端实例。

        刻意不复用全局单例：Client 对象只是装了凭据的轻量配置体，
        每次同步新建一个，就不会出现「设置页测试连接重新 configure 了
        全局实例，正在上传的同步线程拿着换了一半的凭据发请求」的并发污染。

        Raises:
            ValidationError: 配置不完整或客户端构造失败
        """
        config = get_webdav_config()
        url = config.get('url', 'https://dav.jianguoyun.com/dav')
        client = WebDAVClient()
        if not client.configure(config['username'], config['password'],
                                config['remote_path'], url):
            raise ValidationError('WebDAV客户端配置失败')
        return client

    def _prepare_sync(self) -> Tuple[WebDAVClient, str]:
        """准备同步环境，返回 (WebDAV客户端, 本地应用数据目录)。

        成功返回时已持有同步互斥锁，调用方必须在 finally 里 release；
        校验失败时在这里就释放并抛错。
        """
        if not self._sync_op_lock.acquire(blocking=False):
            raise ConflictError('同步正在进行中')
        try:
            if not is_webdav_enabled():
                raise ValidationError('WebDAV未启用')
            client = self._new_client()

            # 本地应用数据目录：<存储目录>/todolist（包含 todo.db 与 attachment）
            from backend.storage.service import get_app_dir
            local_dir = str(get_app_dir())
            return client, local_dir
        except Exception:
            self._sync_op_lock.release()
            raise

    def sync_from_cloud(self, is_overwrite: bool = False) -> int:
        """从云端拉取数据；返回本次实际下载的文件数（0 = 无变化）"""
        client, local_dir = self._prepare_sync()
        self.is_syncing = True
        try:
            downloaded = client.download_app_dir(local_dir, is_overwrite, self.syncs_attachment_files)
            self.last_sync_time = datetime.now()
            if downloaded:
                self.get_logger.info(f"云端数据同步成功，更新 {downloaded} 个文件")
            else:
                # 轮询常态：云端与本地一致，INFO 级每 15s 一条会把日志刷成几 MB，
                # 现象就是「什么都没改 backend.log 也一直在动」
                self.get_logger.debug("云端数据同步完成：无变化")
            if downloaded and self.on_sync_callback:
                try:
                    self.on_sync_callback()
                except Exception as e:
                    self.get_logger.error(f"同步回调执行失败: {e}")
            return downloaded
        finally:
            self.is_syncing = False
            self._sync_op_lock.release()

    def _current_db_path(self) -> str:
        """当前生效的数据库文件路径（供 WAL 落盘等文件级操作使用）。"""
        from backend.storage.service import get_data_file_or_fallback
        return str(get_data_file_or_fallback())

    def _checkpoint_before_upload(self) -> bool:
        """上传前把 WAL 落盘。

        边车文件（todo.db-wal/-shm）不再参与同步，主库文件必须单独成立
        完整数据，否则留在 WAL 里的最后几次写入不会上云。落盘失败说明有
        连接长期占用数据库——宁可不传，也别把缺了尾巴的库推上云端。
        """
        try:
            if checkpoint_wal(self._current_db_path()):
                return True
            self.get_logger.error("WAL 未能落盘（数据库被占用），跳过本次上传")
            return False
        except Exception as e:
            self.get_logger.error(f"上传前 WAL 落盘出错: {e}")
            return False

    def sync_to_cloud(self) -> Dict[str, int]:
        """把本地数据增量上传到云端；返回 {'uploaded':n,'unchanged':n,'skipped':n}"""
        client, local_dir = self._prepare_sync()
        self.is_syncing = True
        try:
            if not os.path.exists(local_dir):
                raise NotFoundError(f'本地数据目录不存在: {local_dir}')
            if not self._checkpoint_before_upload():
                raise DatabaseError('数据库 WAL 未能落盘，本次上传已取消')
            stats = client.upload_app_dir(local_dir, self.syncs_attachment_files)
            self.last_sync_time = datetime.now()
            if isinstance(stats, dict) and stats.get('uploaded'):
                self.get_logger.info(f"数据上传到云端成功，新传 {stats['uploaded']} 个文件")
            else:
                self.get_logger.debug("数据上传完成：内容未变化，已跳过上传")
            return stats
        finally:
            self.is_syncing = False
            self._sync_op_lock.release()

    def download_attachment(self, relative_path: str, local_file: str) -> None:
        """按需下载单个附件（移动端查看附件时使用）

        Args:
            relative_path: 附件相对路径（attachment/YYYY/YYYYMM/YYYYMMDD/xxx）
            local_file: 本地下载目标路径
        """
        if not is_webdav_enabled():
            raise ValidationError('WebDAV未启用')

        client = self._new_client()
        remote_path = client.build_remote_path(relative_path)
        client.download_file(remote_path, local_file, is_overwrite=True, compare_version=False)

    def trigger_upload_on_change(self) -> None:
        """在数据变更时触发上传（防抖：停手 1 秒后才真正上传一次）

        仅「默认（1秒）」频率响应变更；「每小时」「关闭软件时」两种模式下
        上传各有自己的触发时机，写操作不再即时上传。
        """
        config = get_webdav_config()
        if not config.get('enabled', False):
            return
        if config.get('upload_frequency', 'change') != 'change':
            return

        # 防抖合并：连续编辑（如批量导入、逐字输入）时每笔变更都会走到这里，
        # 旧实现每笔各起一个线程 1s 后各传一轮；现在只保留一个定时器，
        # 新变更把窗口重置，上传推迟到停手 1 秒后，且互斥锁保证期间若恰好
        # 撞上轮询同步也只会传一次
        with self._debounce_lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(1.0, self._debounced_upload)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _debounced_upload(self) -> None:
        """防抖窗口到期后的实际上传"""
        with self._debounce_lock:
            self._debounce_timer = None
        try:
            self.sync_to_cloud()
        except Exception as e:
            self.get_logger.error(f"变更时上传失败: {e}")

    def upload_on_exit(self) -> None:
        """关闭软件退出时的收尾上传

        仅上传频率为「关闭软件时」且 WebDAV 已启用时执行；失败只记日志，
        不能因为上传阻塞或报错影响正常退出。
        幂等：托盘退出与主窗口关闭两条路径都会调用这里，只真正上传一次。
        """
        config = get_webdav_config()
        if not config.get('enabled', False):
            return
        if config.get('upload_frequency', 'change') != 'on_exit':
            return
        if self._exit_upload_done.is_set():
            self.get_logger.info("退出前上传已执行过，跳过重复调用")
            return
        self._exit_upload_done.set()

        self.get_logger.info("应用退出，执行退出前上传...")
        try:
            # 15s 轮询可能恰好正在进行：退出上传是最后机会，等它结束再传，
            # 否则非阻塞互斥会直接抛「同步正在进行中」导致本次上传被跳过
            if not self._wait_sync_idle(timeout=15):
                raise ConflictError('等待进行中的同步超时，本次退出上传已放弃')
            stats = self.sync_to_cloud()
            if isinstance(stats, dict) and stats.get('uploaded'):
                self.get_logger.info(f"退出前上传完成，新传 {stats['uploaded']} 个文件")
            else:
                self.get_logger.info("退出前上传完成：内容未变化，无需上传")
        except Exception as e:
            self.get_logger.error(f"退出前上传失败: {e}")

    def _wait_sync_idle(self, timeout: float) -> bool:
        """等待同步互斥锁空闲（有同步在跑时最多等 timeout 秒）

        通过探测式加锁实现：拿到锁说明当前没有同步在执行，立即释放并把
        锁让给随后的 sync_to_cloud（定时链已被 stop_auto_sync 作废，
        不会有新的轮询再来抢锁）。返回 False 表示等到超时仍忙。
        """
        deadline = time.time() + timeout
        while True:
            if self._sync_op_lock.acquire(blocking=False):
                self._sync_op_lock.release()
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.2)

    def get_webdav_config(self) -> Dict[str, Any]:
        """获取WebDAV配置"""
        return get_webdav_config()

    def set_webdav_config(self, config: Dict[str, Any]) -> None:
        """设置WebDAV配置"""
        # 保存配置前，测试数据同步连接是否成功
        url = config.get('url', '')
        username = config.get('username', '')
        password = config.get('password', '')
        remote_path = config.get('remote_path', '')
        self.test_webdav_connection(url, username, password, remote_path)

        # 保存配置
        success = set_webdav_config(config)

        if not success:
            raise DatabaseError(f'配置保存失败')

        # 如果启用了自动同步，重启同步管理器
        if config.get('enabled') and config.get('auto_sync'):
            get_data_sync_manager().start_auto_sync()
        elif not config.get('enabled') or not config.get('auto_sync'):
            # 停止自动同步
            get_data_sync_manager().stop_auto_sync()

    def test_webdav_connection(self, url: str, username: str, password: str, remote_path: str) -> None:
        """测试WebDAV连接"""
        # 用独立实例测试：不影响正在进行的同步（全局单例被 reconfigure 是并发污染源）
        client = WebDAVClient()
        if not client.configure(username, password, remote_path, url):
            raise ValidationError(f'客户端配置失败')
        client.test_connection()

# 全局同步管理器实例
_data_sync_manager: Optional[DataSyncManager] = None

def get_data_sync_manager() -> DataSyncManager:
    """获取全局数据同步管理器实例"""
    global _data_sync_manager
    if _data_sync_manager is None:
        _data_sync_manager = DataSyncManager()
    return _data_sync_manager