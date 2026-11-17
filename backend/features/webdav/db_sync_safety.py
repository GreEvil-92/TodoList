"""数据库文件同步安全工具

WebDAV 同步以文件为单位搬运 todo.db，而 SQLite 运行在 WAL 模式下，
直接按文件覆盖存在两个数据损坏风险：

1. ``todo.db-wal`` / ``todo.db-shm`` 边车文件被一并上传/下载。云端一份陈旧
   WAL 套上新的主库文件（或反过来）会被 SQLite 当作合法恢复数据套用，
   直接写坏库。
2. 下载覆盖主库时应用仍在运行。常驻只读连接（桌面插件）等还开着旧文件：
   Windows 上覆盖直接失败，POSIX 上旧连接继续读旧 inode 出现「幽灵数据」；
   更糟的是覆盖后残留的旧 WAL 会被套用到新主库上。

本模块集中提供两件事：

- :func:`checkpoint_wal`：上传前把 WAL 里尚未落盘的页合并回主库，
  保证主库文件单独成立完整数据（边车不再参与同步后，这是数据上云的前提）。
- :func:`safe_replace_live_db`：下载完成后把云端库安全换入正在使用的库：
  挂起插件只读连接 → checkpoint 清空 WAL → 备份 → 原子替换 → 完整性校验，
  任一步失败回滚到备份，不留半新半旧的文件。
"""

import os
import shutil
import sqlite3
import time

from backend.features.webdav.sync_fingerprint import is_state_file

# WAL 模式的边车文件后缀（紧跟在主库文件名之后，如 todo.db-wal / todo.db-shm）
DB_SIDECAR_SUFFIXES = ('-wal', '-shm')
# 本模块下载临时文件 / 备份文件的后缀，这些文件一律不参与云同步
SYNC_TMP_SUFFIX = '.sync-tmp'
SYNC_BAK_SUFFIX = '.sync-bak'

# os.replace 在 Windows 上目标被占用时会抛 PermissionError；短命连接
# （tx()/query() 每次开关）只在毫秒级持有文件，重试即可等到释放
_REPLACE_RETRIES = 10
_REPLACE_RETRY_DELAY = 0.2


def is_sync_excluded_file(relative_name: str) -> bool:
    """相对应用数据目录的文件名是否不应参与云同步。

    覆盖四类：WAL 边车（套用到另一个主库上会写坏库）、换库临时文件、
    换库备份文件、上传指纹记录（纯本地状态，传到云端毫无意义）。
    """
    name = str(relative_name).replace('\\', '/')
    # 只拦数据目录顶层的 todo.db-wal 这类文件；附件目录下同名后缀不受影响
    if '/' in name:
        return False
    base = os.path.basename(name)
    if is_state_file(base):
        return True
    return (base.endswith(DB_SIDECAR_SUFFIXES)
            or base.endswith((SYNC_TMP_SUFFIX, SYNC_BAK_SUFFIX)))


def _db_path_of(db_path: str, suffix: str) -> str:
    return str(db_path) + suffix


def checkpoint_wal(db_path: str, attempts: int = 5, delay: float = 0.3) -> bool:
    """把 WAL 中未落盘的页合并回主库（TRUNCATE 模式，成功后 WAL 清零）。

    主库文件「单独成立完整数据」是上传上云的前提——边车不同步，留在 WAL
    里的最后几次写入必须先落盘，否则云端拿到的是缺了尾巴的库。

    失败的常见原因是还有别的连接正持有读事务（毫秒级），重试即可等到。
    全部重试失败时返回 False，调用方应放弃本次上传而不是带着缺口上云。

    Returns:
        bool: 是否成功落盘（WAL 不存在或已清零都算成功）
    """
    wal_path = _db_path_of(db_path, '-wal')

    for _ in range(attempts):
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                # 返回 (busy, log页数, checkpoint页数)；busy=0 表示完整合并
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            finally:
                conn.close()
        except Exception:
            # 连不上就当落盘失败处理，交给重试
            time.sleep(delay)
            continue

        # 连接全部关闭后 SQLite 会自动回收 WAL 文件
        if not os.path.exists(wal_path) or os.path.getsize(wal_path) == 0:
            return True
        time.sleep(delay)

    # 走到这里说明 TRUNCATE 一直没完成（busy != 0 或连接异常），以 WAL 文件状态为准
    return False


def _replace_with_retry(src: str, dst: str,
                        attempts: int = _REPLACE_RETRIES,
                        delay: float = _REPLACE_RETRY_DELAY) -> None:
    """os.replace 带重试：目标被其他短命连接短暂占用时等一等再换。"""
    last_err: Optional[Exception] = None
    for _ in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last_err = e
        except OSError as e:
            # Windows 上目标被占用也可能以 OSError 形式出现
            last_err = e
        time.sleep(delay)
    raise last_err if last_err else OSError('数据库文件替换失败')


def _remove_sidecars(db_path: str) -> None:
    """删除主库旁残留的边车文件。

    换库后残留的旧 WAL 会被 SQLite 套用到新主库上，必须清掉。若此刻
    WAL 已有新写入（毫秒级竞态），删除意味着丢弃这一次写入——远好于
    让陈旧页套坏新库。
    """
    for suffix in DB_SIDECAR_SUFFIXES:
        try:
            os.remove(_db_path_of(db_path, suffix))
        except FileNotFoundError:
            pass
        except OSError:
            pass  # 删不掉也别挡住主流程，校验环节会兜底


def _quick_check(db_path: str) -> bool:
    """对新库做一次快速完整性校验。"""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute('PRAGMA quick_check').fetchone()
            return bool(row) and str(row[0]).lower() == 'ok'
        finally:
            conn.close()
    except Exception:
        return False


def _suspend_widget_reader() -> bool:
    """挂起桌面插件的常驻只读连接，腾出文件替换窗口。

    插件只在桌面平台存在，且未显示时连接本来就是关的；其余情况一律
    静默跳过，不能因为找不到插件阻塞同步。
    """
    try:
        from backend.platforms.impl.desktop.common.desktop_widget import DesktopWidget
        instance = DesktopWidget._instance
        if instance is not None:
            instance._reader.suspend()
            return True
    except Exception:
        pass
    return False


def _resume_widget_reader() -> None:
    try:
        from backend.platforms.impl.desktop.common.desktop_widget import DesktopWidget
        instance = DesktopWidget._instance
        if instance is not None:
            instance._reader.resume()
    except Exception:
        pass


def safe_replace_live_db(tmp_path: str, db_path: str) -> None:
    """把下载好的数据库临时文件安全换入正在使用的库。

    步骤（任一步失败都会回滚，不留半新半旧的文件）：

    1. 挂起插件只读连接（桌面平台，未运行时跳过）；
    2. checkpoint 清空现库 WAL——若清不掉说明有连接长期占用，此时换库
       会把残留 WAL 套到新库上，必须放弃而不是硬换；
    3. 备份现库（checkpoint 后主库文件已完整，单文件备份即可）；
    4. 原子替换（带重试），随后清掉换库瞬间可能残留的边车；
    5. 对新库做完整性校验；失败则用备份回滚。

    Raises:
        RuntimeError: 换库未成功，本地仍保持原库（或回滚后的原库）。
    """
    suspended = _suspend_widget_reader()
    backup_path = _db_path_of(db_path, SYNC_BAK_SUFFIX)
    try:
        if not checkpoint_wal(str(db_path)):
            raise RuntimeError(
                'WAL 未能落盘（数据库正被占用），已放弃换库以避免数据损坏')

        # 备份：换库或校验失败时的回滚来源
        try:
            shutil.copyfile(str(db_path), backup_path)
        except OSError as e:
            raise RuntimeError(f'换库前备份失败，已放弃换库: {e}')

        try:
            _replace_with_retry(str(tmp_path), str(db_path))
            _remove_sidecars(db_path)
            if not _quick_check(str(db_path)):
                raise RuntimeError('换库后完整性校验失败')
        except Exception:
            # 回滚：备份盖回去，边车同样清掉，让本地回到换库前的状态
            try:
                _replace_with_retry(backup_path, str(db_path))
                _remove_sidecars(db_path)
            except Exception as rollback_err:
                raise RuntimeError(
                    f'换库失败且回滚失败，请用备份文件手动恢复: {backup_path}（{rollback_err}）')
            raise

        # 全部成功，清理备份（删不掉不影响结果，只是留个文件）
        try:
            os.remove(backup_path)
        except OSError:
            pass
    finally:
        if suspended:
            _resume_widget_reader()
