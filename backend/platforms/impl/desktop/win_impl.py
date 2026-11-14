# impl/desktop/win_impl.py
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from backend.platforms.impl.desktop.common.common_impl import DesktopCommonService
from backend.platforms.impl.desktop.win_firewall_manager import FirewallManager

class WindowsService(DesktopCommonService):
    def __init__(self):
        super().__init__()
        self._mutex_handle = None  # 用于保存互斥体句柄

    def shortcut_handler(self, shortcut: str, handler: Callable[[], None]) -> Optional[Any]:
        try:
            import backend.globals
            from pynput import keyboard
            listener = keyboard.GlobalHotKeys({shortcut: handler})
            listener.start()
            self.backend_logger().info(f"【系统日志】快捷键监听成功挂载！当前在 Mac 下的标准热键为: {shortcut}")
            return None
        except Exception as e:
            self.backend_logger().error(f"【系统日志】快捷键挂载失败: {e}")

    def force_kill_process_tree(self, pid: int) -> None:
        """强制结束当前进程及其所有子进程的统一接口"""
        import subprocess
        import time
        import win32api

        if self._mutex_handle:
            win32api.CloseHandle(self._mutex_handle)
        # --- Windows ---
        # 优雅终止 (SIGTERM)
        subprocess.run(f'taskkill /PID {pid} /T', shell=True)
        time.sleep(2)
        # 强制终止 (SIGKILL)
        subprocess.run(f'taskkill /F /T /PID {pid}', shell=True, capture_output=True)

    def get_config_dir(self) -> Path:
        """Windows 惯例：%APPDATA%/TodoList"""
        from backend.utils.utils import APP_DIR_NAME, ensure_dir
        return ensure_dir(Path(os.environ.get('APPDATA') or Path.home()) / APP_DIR_NAME)

    def _packaged_data_dir(self) -> Path:
        """打包后数据与配置同根，放 %APPDATA%/TodoList"""
        return self.get_config_dir()

    def _packaged_log_dir(self) -> Path:
        """打包后写 exe 同级目录（用户通常对该目录有写权限）"""
        return Path(sys.executable).parent / 'logs'

    def get_app_icon(self, base_path: Path) -> Path:
        """获取应用图标的统一接口"""
        return base_path / 'todo_icon.ico'

    def activate_existing_window(self) -> bool:
        # 使用窗口标题查找，确保与创建时一致
        import win32gui
        import win32con
        hwnd = win32gui.FindWindow(None, self.APP_NAME)
        if hwnd:
            # 1. 如果窗口不可见（被 hide），则显示它
            if not win32gui.IsWindowVisible(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            # 2. 如果窗口被最小化，则还原
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            # 3. 将窗口置于前台
            win32gui.SetForegroundWindow(hwnd)
            # 4. （可选）强制置顶一次，避免被其他窗口遮挡
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            return True
        return False

    def hide_taskbar_icon(self, window: Any) -> None:
        """将快捷键窗口从 Windows 任务栏中隐藏，任务栏只保留主窗口"""
        try:
            native = getattr(window, 'native', None)
            if native is None:
                return

            # 注意：native.Handle 是 pythonnet 的 IntPtr，ctypes 无法直接识别，必须先转成 int
            hwnd = native.Handle.ToInt32()
            if not hwnd:
                return

            import ctypes

            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000

            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020

            user32 = ctypes.windll.user32
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            # 添加 WS_EX_TOOLWINDOW 并移除 WS_EX_APPWINDOW，使窗口不在任务栏显示
            ex_style = (ex_style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
            # 通知系统窗口扩展样式已变更
            user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
            )
            self.backend_logger().info("快捷键窗口已从任务栏隐藏，仅保留主窗口任务栏图标")
        except Exception as e:
            self.backend_logger().error(f"隐藏快捷键窗口任务栏图标失败: {e}")

    def set_title_bar_mode(self, dark: bool) -> bool:
        """把主窗口标题栏与边框切成深色/浅色（DWM 沉浸式深色模式）。

        只做深浅两态：DWMWA_BORDER_COLOR / DWMWA_CAPTION_COLOR 是 Windows 11（22000+）
        才有的属性，在 Windows 10 上调用直接返回失败，所以自定义边框颜色这条路在本机
        系统上走不通，深浅跟随主题是当前能做到的最大同步。

        属性号 20 适用于 19041/19042/19045（2004 及之后），更早的 18985~18362 用 19，
        因此先试 20、失败再试 19。
        """
        import ctypes

        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0001, 0x0002, 0x0004, 0x0010, 0x0020

        try:
            import backend.globals

            window = backend.globals.window
            if window is None:
                return False

            native = getattr(window, 'native', None)
            if native is None:
                return False
            # native.Handle 是 pythonnet 的 IntPtr，ctypes 需要先转 int
            hwnd = native.Handle.ToInt32()
            if not hwnd:
                return False

            value = ctypes.c_int(1 if dark else 0)
            dwmapi = ctypes.windll.dwmapi
            applied = False
            for attribute in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY):
                result = dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
                if result == 0:
                    applied = True
                    break

            if applied:
                # DWM 不会自己重画标题栏：属性写进去后必须显式让非客户区重建一次，
                # 否则要等用户手动缩放/最大化才看到新颜色（Win10 上的已知行为）。
                # 单靠 SWP_FRAMECHANGED 不足以触发重建，还要原样重写一次窗口样式，
                # 最后用 RDW_FRAME 强制刷新非客户区。
                user32 = ctypes.windll.user32

                GWL_STYLE = -16
                RDW_INVALIDATE, RDW_ALLCHILDREN, RDW_UPDATENOW, RDW_FRAME = 0x0001, 0x0080, 0x0100, 0x0400

                user32.SetWindowLongW(hwnd, GWL_STYLE, user32.GetWindowLongW(hwnd, GWL_STYLE))
                user32.SetWindowPos(
                    hwnd, 0, 0, 0, 0, 0,
                    SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
                user32.RedrawWindow(
                    hwnd, None, None,
                    RDW_INVALIDATE | RDW_FRAME | RDW_UPDATENOW | RDW_ALLCHILDREN)

                # 上面三连在部分 Win10 上仍不足以刷新标题栏，只有发生一次真实的尺寸变化
                # （用户手动缩放）才会重建。这里补一次 1 像素的尺寸往返微扰。
                self._nudge_window_frame(hwnd, user32)

                self.backend_logger().info(f"主窗口标题栏已切换为{'深色' if dark else '浅色'}")
            return applied
        except Exception as e:
            self.backend_logger().warning(f"设置主窗口标题栏深浅失败: {e}")
            return False

    def _nudge_window_frame(self, hwnd: int, user32: Any) -> None:
        """用 1 物理像素的尺寸往返触发 DWM 重建非客户区。

        - SetWindowPos 直接走 user32，与 GetWindowRect 同为物理像素坐标系，
          因此不需要做 DPI 换算；SWP_NOMOVE 保证位置不动，最终尺寸与原来一致。
        - 最大化时改尺寸会直接退出最大化、最小化时改了也看不见，这两种状态跳过。
        """
        import ctypes

        if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
            return

        class RECT(ctypes.Structure):
            _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                        ('right', ctypes.c_long), ('bottom', ctypes.c_long)]

        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return

        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return

        SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0002, 0x0004, 0x0010, 0x0020
        flags = SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED

        user32.SetWindowPos(hwnd, 0, 0, 0, width + 1, height, flags)
        user32.SetWindowPos(hwnd, 0, 0, 0, width, height, flags)

    def start_prepare(self) -> None:
        """应用启动前准备工作的统一接口"""
        import sys
        import win32event
        import win32api
        import winerror

        try:
            mutex = win32event.CreateMutex(None, False, self.APP_NAME)
            last_error = win32api.GetLastError()
            if last_error == winerror.ERROR_ALREADY_EXISTS:
                self.backend_logger().info("检测到已有实例，尝试激活窗口...")
                self.activate_existing_window()
                sys.exit(0)
            else:
                # 首次启动，保存互斥体句柄
                self._mutex_handle = mutex
                self.backend_logger().info("首次启动，已创建互斥体并保存句柄")
        except Exception as e:
            self.backend_logger().error(f"互斥体操作失败: {e}")
            # 如果创建失败，仍然允许启动，但可能导致多实例

    def add_firewall_rule(self, port: int) -> Tuple[bool, str]:
        """添加防火墙策略规则的统一接口"""
        firewall_manager = FirewallManager(port=port)
        return firewall_manager.add_rule()

    def remove_firewall_rule(self, port: int) -> Tuple[bool, str]:
        """移除防火墙策略规则的统一接口"""
        firewall_manager = FirewallManager(port=port)
        return firewall_manager.remove_rule()

    def _enable_auto_start_impl(self) -> bool:
        """启用开机自启动"""
        from backend.utils import utils
        app_path = utils.get_app_path(self)

        try:
            import winreg

            # 启动命令
            launch_cmd = utils.get_launch_command(self)

            # 注册表路径
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"

            # 打开注册表键
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
                # 设置注册表值
                winreg.SetValueEx(key, self.APP_NAME, 0, winreg.REG_SZ, launch_cmd)

            self.backend_logger().info(f"Windows开机自启动已启用: {launch_cmd}")
            return True

        except ImportError:
            # 备用方案：使用启动文件夹
            startup_folder = Path(
                os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
            startup_folder.mkdir(parents=True, exist_ok=True)

            # 创建快捷方式
            shortcut_path = startup_folder / f"{self.APP_NAME}.lnk"

            # 使用Python创建快捷方式
            import pythoncom
            from win32com.client import Dispatch

            shell = Dispatch('WScript.Shell')
            shortcut = shell.CreateShortcut(str(shortcut_path))
            shortcut.Targetpath = app_path
            shortcut.WorkingDirectory = str(Path(app_path).parent)

            shortcut.save()

            self.backend_logger().warning(f"Windows启动文件夹快捷方式已创建: {shortcut_path}")

            return True
        except Exception as e:
            self.backend_logger().error(f"启用开机自启动失败: {e}")
            return False

    def _disable_auto_start_impl(self) -> bool:
        """禁用开机自启动"""
        try:
            import winreg

            # 注册表路径
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"

            # 尝试删除注册表项
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
                    winreg.DeleteValue(key, self.APP_NAME)
            except FileNotFoundError:
                # 注册表项不存在，继续检查启动文件夹
                pass

            # 删除启动文件夹中的快捷方式和批处理文件
            startup_folder = Path(
                os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'

            # 删除快捷方式
            shortcut_path = startup_folder / f"{self.APP_NAME}.lnk"
            if shortcut_path.exists():
                shortcut_path.unlink()

            # 删除批处理文件
            bat_path = startup_folder / f"{self.APP_NAME}.bat"
            if bat_path.exists():
                bat_path.unlink()

            self.backend_logger().info("Windows开机自启动已禁用")
            return True

        except Exception as e:
            self.backend_logger().error(f"Windows禁用自启动失败: {e}")
            return False

# 用于给工厂注册的导出变量
ExportService = WindowsService
