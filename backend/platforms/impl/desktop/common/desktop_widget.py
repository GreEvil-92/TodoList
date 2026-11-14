# backend/platforms/impl/desktop/common/desktop_widget.py
"""
桌面显示插件：默认 360×120 的无边框置顶小窗（宽高比固定 3:1），显示所选优先级范围的任务信息
（设置页可配：优先级范围 + 倒计时区间，见 _WIDGET_PRIORITY_KEYS / _WIDGET_RANGE_KEYS）。

由主界面侧边栏的「桌面显示插件」按钮开关，入口见 backend/api/mixins/widget_api_mixin.py。

页面内容（两格左右排布，中间竖向分隔线）：
- 左格（约 1/3 宽）：未完成任务数量（所选优先级范围内的全部未完成，设置页可配范围）；
- 右格（约 2/3 宽）：只要存在逾期任务就直接显示「已逾期x个」（不再显示倒计时）；
        否则显示「剩余倒计时」hh:mm:ss；今日没有高优先级任务时显示「无事发生」。

配色（整个窗口背景，随剩余时间分 10 档，见 _countdown_level_limits / _countdown_level_colors）：
- 每档背景色由用户逐档指定，不做插值；剩余 >= 2 小时或无待办任务为 #28a745，
  剩余 < 30 分钟或存在逾期任务为 #dc3545；
- 文字色按背景感知亮度自动取深/白，保证橙黄绿这些亮档也看得清。

交互：
- 双击插件任意位置 → 唤起主窗口；
- 四个角可等比缩放（按住某个角，对角保持不动）；
- 空白处按住拖动可移动窗口（pywebview 的 easy_drag 只认 .pywebview-drag-region）。

设计要点：
- 窗口只创建一次并常驻内存，「关闭」只是 hide，再次点击立即 show，避免反复创建 WebView；
- 倒计时与配色都由页面本地每秒推进，后端每 2 秒重新推送一次做校准，
  因此既不走字，也不会因为后台线程每秒查库而增加开销；插件隐藏时刷新线程立即停止。

只读取、不动存储（本次改动的核心，详见 ReadOnlyDatabaseReader）：
- 插件显示的全部内容都可以由「数据库现有信息 + 系统时间」推出来，不需要写任何东西；
- 取数走一条常驻的只读连接（``PRAGMA query_only = ON``），写操作在 SQLite 层就被拒绝；
- 不再每 2 秒「开一条连接、查完关掉」：WAL 模式下关闭最后一条连接会在数据目录里
  创建再删除 ``todo.db-wal`` / ``todo.db-shm``，网盘同步客户端按目录事件判断
  「文件变了」，于是表现为插件一直在改存储、一直在上传；
- 也不再在构造时建 ``TodoDatabase``（那会顺带跑一遍建表 / 补列 / 孤儿清理），
  只在只读连接不可用时才惰性创建它兜底。
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from backend.database.mixins.setting_crud_mixin import (
    SettingCrudMixin,
    query_setting_value,
)
from backend.database.mixins.statistics_crud_mixin import (
    query_desktop_widget_statistics,
)
from backend.utils.logger import LogManager

app_name_widget: str = 'TodoList_DesktopWidget'
widget_width: int = 360
widget_height: int = 120
# 宽高比固定 3:1：左格占 1/3 宽（恰为正方形），右格占 2/3 宽（横向长方形，容纳倒计时）；
# 所有字号/间距都用 vh/% 表达，等比缩放时布局不会变形
widget_aspect: float = 3.0
# 缩放下限/上限均指「宽度」；高度 = 宽度 / widget_aspect
widget_min_size: int = 240
widget_max_size: int = 900
refresh_interval: float = 2.0

# 只读连接的建连等待时间（秒）：只读查询不必陪写锁久等，拿不到就本轮跳过、下轮再试
_reader_connect_timeout: float = 2.0

# 时间阶梯：10 档，前 9 档是「小于」，最后一档是「大于或等于 2 小时」。
# 这里把「小于 1:50」与「大于或等于 2:00」之间的空档并入第 9 档（上限取 2:00），
# 保证任何剩余秒数都能落到唯一一档，不会出现无颜色的空隙。
_countdown_level_limits: Tuple[int, ...] = (
    30 * 60, 40 * 60, 50 * 60, 60 * 60, 70 * 60,
    80 * 60, 90 * 60, 100 * 60, 120 * 60,
)

# 每档背景色按用户给定的表逐档写死（不再插值），与上面的上限一一对应；
# 第 10 档没有上限，「>= 2 小时」与「无待办任务」都用它。
_countdown_level_colors: Tuple[str, ...] = (
    '#dc3545',   # 小于 0:30:00
    '#e35438',   # 小于 0:40:00
    '#eb732a',   # 小于 0:50:00
    '#f3921c',   # 小于 1:00:00
    '#fbb10e',   # 小于 1:10:00
    '#e8bf0d',   # 小于 1:20:00
    '#b8b91b',   # 小于 1:30:00
    '#88b329',   # 小于 1:40:00
    '#58ad37',   # 小于 1:50:00
    '#28a745',   # 大于或等于 2:00:00
)

# 插件文案随应用语言切换，避免界面是英文而插件永远是中文
_widget_labels: Dict[str, Dict[str, str]] = {
    'zh': {'today': '未完成', 'countdown': '剩余倒计时',
           'overdue': '已逾期{0}个', 'idle': '无事发生'},
    'en': {'today': 'Unfinished', 'countdown': 'Time remaining',
           'overdue': '{0} overdue', 'idle': 'Nothing to do'},
}

# 插件可选的任务优先级（与 tasks.priority 的取值一致）与倒计时区间（设置页可配）
_WIDGET_PRIORITY_KEYS: Tuple[str, ...] = ('high', 'medium', 'low', 'none')
_WIDGET_RANGE_KEYS: Tuple[str, ...] = ('today', 'tomorrow', 'week', 'month')
# 未配置时保持历史口径：只看高优先级，倒计时取任意未来任务里最近的一个
_WIDGET_DEFAULT_PRIORITIES: Tuple[str, ...] = ('high',)
_WIDGET_DEFAULT_RANGE: str = 'month'


def _normalize_priorities(value: Any) -> Tuple[str, ...]:
    """把设置值规整成有效的优先级元组（去重、保序、过滤非法值）。

    允许为空：四个都不选 = 范围内没有任何任务，插件恒显示「无事发生」。
    """
    if isinstance(value, str):
        candidates = value.split(',')
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        return _WIDGET_DEFAULT_PRIORITIES

    seen: List[str] = []
    for item in candidates:
        key = str(item).strip().lower()
        if key in _WIDGET_PRIORITY_KEYS and key not in seen:
            seen.append(key)
    return tuple(seen)


def _countdown_range_end(now: datetime, range_key: str) -> Optional[datetime]:
    """倒计时区间的上限（排他）：区间内 = [此刻, 上限)。

    今日 = 今天 24:00 前；明日 = 明天 24:00 前（含今天剩余时间）；
    本周内 = 下周一 00:00 前（周一为一周起点）；本月内 = 下月 1 日 00:00 前。
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key == 'today':
        return midnight + timedelta(days=1)
    if range_key == 'tomorrow':
        return midnight + timedelta(days=2)
    if range_key == 'week':
        return midnight + timedelta(days=7 - now.weekday())
    if range_key == 'month':
        if now.month == 12:
            return midnight.replace(year=now.year + 1, month=1, day=1)
        return midnight.replace(month=now.month + 1, day=1)
    return None


def _resolve_widget_scope(raw_priorities: Any, raw_range: Any,
                          now: datetime) -> Tuple[Tuple[str, ...], Optional[str]]:
    """把两条原始设置值解析成取数参数：`(优先级元组, 区间上限串或 None)`。

    解析失败一律退回默认口径（只看高优先级 + 不设上限），保证插件总能出数。
    """
    priorities = (_normalize_priorities(raw_priorities)
                  if raw_priorities is not None else _WIDGET_DEFAULT_PRIORITIES)
    range_key = str(raw_range).strip().lower() if raw_range is not None else ''
    if range_key not in _WIDGET_RANGE_KEYS:
        range_key = _WIDGET_DEFAULT_RANGE
    end = _countdown_range_end(now, range_key)
    range_end = end.isoformat(sep=' ', timespec='seconds') if end else None
    return priorities, range_end


def _hex_to_rgb(value: str) -> Tuple[int, int, int]:
    """'#dc3545' -> (220, 53, 69)"""
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def _contrast_color(rgb: Tuple[int, int, int]) -> str:
    """按感知亮度决定文字用深色还是白色，保证黄底也能看清。"""
    luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    return '#111111' if luminance > 150 else '#ffffff'


def _build_color_levels() -> List[Dict[str, Any]]:
    """把档位表推给页面，页面每秒本地换色，不必回头端逐秒查库。"""
    levels: List[Dict[str, Any]] = []
    for index, hex_value in enumerate(_countdown_level_colors):
        levels.append({
            'limit': _countdown_level_limits[index] if index < len(_countdown_level_limits) else None,
            'bg': hex_value,
            'fg': _contrast_color(_hex_to_rgb(hex_value)),
        })
    return levels


widget_html: str = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { width: 100%; height: 100%; overflow: hidden; }
        body {
            position: relative;
            display: flex;
            flex-direction: row;
            /* 背景色与文字色由后端档位决定，初始给最宽松的一档 */
            background: #28a745;
            color: #ffffff;
            font-family: 'Segoe UI', 'Microsoft YaHei', system-ui, -apple-system, sans-serif;
            user-select: none;
            cursor: default;
            transition: background-color 0.4s linear;
        }
        .cell {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 2%;
            overflow: hidden;
            padding: 0 2%;
        }
        /* 左格正方形：窗口宽高比 3:1，占 1/3 宽度即与格高相等；右格为 2 倍格高的横向长方形 */
        .cell.left { flex: 0 0 33.333%; }
        /* 竖向分隔线随背景色深浅反色，不用固定灰色 */
        .cell + .cell { border-left: 1px solid var(--divider, rgba(0, 0, 0, 0.18)); }
        .value {
            font-size: 52vh;
            font-weight: 600;
            line-height: 1;
            white-space: nowrap;
        }
        .label { font-size: 10vh; opacity: 0.75; white-space: nowrap; }
        /* 倒计时是 hh:mm:ss 长串：右格宽约为 2 倍格高，字号取 32vh 让比例舒展不顶边 */
        .cell.bottom.is-countdown .value { font-size: 32vh; }
        /* 「已逾期x个」：与倒计时同字号，右格两种状态视觉一致 */
        .cell.bottom.is-overdue .value { font-size: 32vh; }
        /* 「无事发生」：今日没有高优先级任务时的空态，与其它状态同字号 */
        .cell.bottom.is-idle .value { font-size: 32vh; }
        /* 四角缩放手柄：不属于拖拽区域，所以 easy_drag 不会抢走按下事件 */
        .rs { position: absolute; width: 16px; height: 16px; z-index: 10; }
        .rs.tl { top: 0; left: 0; cursor: nwse-resize; }
        .rs.tr { top: 0; right: 0; cursor: nesw-resize; }
        .rs.bl { bottom: 0; left: 0; cursor: nesw-resize; }
        .rs.br { bottom: 0; right: 0; cursor: nwse-resize; }
    </style>
</head>
<body>
    <div class="cell left pywebview-drag-region">
        <span class="value" id="today-value">-</span>
        <span class="label" id="today-label">未完成</span>
    </div>
    <!-- 初始静态态与「无事发生」一致：首帧推送前的占位不会闪出一个不存在的状态 -->
    <div class="cell bottom is-idle pywebview-drag-region" id="bottom-cell">
        <span class="value" id="bottom-value">无事发生</span>
        <span class="label" id="bottom-label"></span>
    </div>

    <div class="rs tl" data-corner="tl"></div>
    <div class="rs tr" data-corner="tr"></div>
    <div class="rs bl" data-corner="bl"></div>
    <div class="rs br" data-corner="br"></div>

    <script>
        (function () {
            var body = document.body;
            var todayValue = document.getElementById('today-value');
            var todayLabel = document.getElementById('today-label');
            var bottomCell = document.getElementById('bottom-cell');
            var bottomValue = document.getElementById('bottom-value');
            var bottomLabel = document.getElementById('bottom-label');

            var state = {
                mode: 'idle',          // countdown / overdue / idle
                remaining: 0,
                overDue: 0,
                labels: {},
                levels: []
            };

            function pad(value) { return value < 10 ? '0' + value : String(value); }

            function formatCountdown(total) {
                var hours = Math.floor(total / 3600);
                var minutes = Math.floor((total % 3600) / 60);
                var seconds = total % 60;
                // 超过一天时小时位继续累加（如 30:05:00），不折成天数
                return pad(hours) + ':' + pad(minutes) + ':' + pad(seconds);
            }

            function levelIndexFor(seconds) {
                for (var i = 0; i < state.levels.length; i++) {
                    if (state.levels[i].limit === null || seconds < state.levels[i].limit) return i;
                }
                return state.levels.length - 1;
            }

            function applyTheme(index) {
                if (!state.levels.length) return;
                var level = state.levels[Math.max(0, Math.min(index, state.levels.length - 1))];
                body.style.backgroundColor = level.bg;
                body.style.color = level.fg;
                // 深底用浅色分隔线，浅底用深色分隔线
                body.style.setProperty('--divider',
                    level.fg === '#ffffff' ? 'rgba(255, 255, 255, 0.28)' : 'rgba(0, 0, 0, 0.18)');
            }

            function renderBottom() {
                bottomCell.classList.remove('is-countdown', 'is-overdue', 'is-idle');
                bottomValue.style.fontSize = '';

                if (state.mode === 'overdue') {
                    bottomCell.classList.add('is-overdue');
                    bottomValue.textContent = (state.labels.overdue || '已逾期{0}个')
                        .replace('{0}', ' ' + state.overDue + ' ');
                    bottomLabel.textContent = '';
                    // 逾期一律按最紧急的一档着色
                    applyTheme(0);
                    return;
                }

                if (state.mode === 'countdown') {
                    bottomCell.classList.add('is-countdown');
                    bottomValue.textContent = formatCountdown(state.remaining);
                    bottomLabel.textContent = state.labels.countdown || '剩余倒计时';
                    applyTheme(levelIndexFor(state.remaining));
                    return;
                }

                // 今日没有高优先级任务：右格整块只显示「无事发生」，保持最宽松的一档（绿）
                bottomCell.classList.add('is-idle');
                bottomValue.textContent = state.labels.idle || '无事发生';
                bottomLabel.textContent = '';
                fitIdleValue();
                applyTheme(state.levels.length - 1);
            }

            // 「无事发生」是整句文案，中英长度差很多：按右格可用宽度从 32vh 逐级收字号，
            // 保证英文（Nothing to do）在窄窗口下也不会被裁掉；其余状态是定长数字，用 CSS 字号即可。
            function fitIdleValue() {
                if (state.mode !== 'idle') return;
                var style = getComputedStyle(bottomCell);
                var cellWidth = bottomCell.clientWidth
                    - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
                if (!cellWidth) return;
                var size = 32;
                bottomValue.style.fontSize = size + 'vh';
                while (size > 10 && bottomValue.getBoundingClientRect().width > cellWidth) {
                    size -= 1;
                    bottomValue.style.fontSize = size + 'vh';
                }
            }

            // 后端推送入口：首次显示、每秒到点、以及每 2 秒的校准都走这里
            window.__widgetRender = function (data) {
                if (!data) return;
                todayValue.textContent = data.unfinished;
                state.overDue = data.over_due;
                state.labels = data.labels || {};
                if (data.levels && data.levels.length) state.levels = data.levels;
                state.mode = data.mode || 'idle';
                state.remaining = Math.max(0, data.remaining || 0);
                if (state.labels.today) todayLabel.textContent = state.labels.today;
                renderBottom();
            };

            setInterval(function () {
                if (state.mode !== 'countdown') return;
                state.remaining -= 1;
                if (state.remaining > 0) {
                    renderBottom();
                    return;
                }
                // 走字到 0：立刻向后端要一次最新数据，由后端决定切到「已逾期」还是下一个任务
                var api = window.pywebview && window.pywebview.api;
                if (api && api.get_widget_data) {
                    api.get_widget_data().then(window.__widgetRender);
                }
            }, 1000);

            // 窗口缩放后右格宽度变了，空态文案需要重新按宽度收字号
            window.addEventListener('resize', fitIdleValue);

            // 双击唤起主界面（缩放手柄上的双击除外）
            document.addEventListener('dblclick', function (event) {
                if (event.target.closest && event.target.closest('.rs')) return;
                var api = window.pywebview && window.pywebview.api;
                if (api && api.focus_main_window) api.focus_main_window();
            });

            // 四角等比缩放：JS 只算目标边长，位置锚定交给后端（pywebview 的 fix_point）
            var cornerSigns = { tl: [-1, -1], tr: [1, -1], bl: [-1, 1], br: [1, 1] };

            document.querySelectorAll('.rs').forEach(function (handle) {
                handle.addEventListener('mousedown', function (event) {
                    var api = window.pywebview && window.pywebview.api;
                    if (!api || !api.update_widget_resize) return;

                    var signs = cornerSigns[handle.dataset.corner];
                    // 宽高比固定 3:1，JS 只上报目标宽度，高度由后端按比例换算
                    var startSize = document.documentElement.clientWidth;
                    var startX = event.screenX;
                    var startY = event.screenY;

                    api.begin_widget_resize(handle.dataset.corner);

                    function onMove(ev) {
                        var dx = ev.screenX - startX;
                        var dy = ev.screenY - startY;
                        // 取位移更大的那个轴，保证斜着拖也是等比变化
                        var delta = Math.abs(dx) >= Math.abs(dy) ? dx * signs[0] : dy * signs[1];
                        api.update_widget_resize(startSize + delta);
                    }

                    function onUp() {
                        window.removeEventListener('mousemove', onMove);
                        window.removeEventListener('mouseup', onUp);
                        api.end_widget_resize();
                    }

                    window.addEventListener('mousemove', onMove);
                    window.addEventListener('mouseup', onUp);
                    event.preventDefault();
                    event.stopPropagation();
                });
            });

            // 主动取一次数据，避免刚显示时停在占位符上。
            // pywebviewready 之前 window.pywebview.api 还不存在，所以两条路各试一次；
            // 万一都没成功，后端每 2 秒的校准推送会兜住。
            function boot() {
                var api = window.pywebview && window.pywebview.api;
                if (api && api.get_widget_data) {
                    api.get_widget_data().then(window.__widgetRender);
                }
            }
            window.addEventListener('pywebviewready', boot);
            boot();
        })();
    </script>
</body>
</html>
"""


def _parse_due(value: Any) -> Optional[datetime]:
    """解析 SQLite datetime() 输出的 'YYYY-MM-DD HH:MM:SS'，失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _language_code(value: Any) -> str:
    """把设置里的语言值归一成插件文案表支持的键，认不出的一律按中文。"""
    lang = str(value or '').strip().lower()
    return lang if lang in _widget_labels else 'zh'


class ReadOnlyDatabaseReader:
    """插件专用的常驻只读连接。

    与「随手 new 一个 TodoDatabase 然后每 2 秒查一次」相比，差别就是这次要解决的两件事：

    1. **写不进去**。建好连接立刻 ``PRAGMA query_only = ON``，此后这条连接上的
       INSERT / UPDATE / DELETE / DDL 都会被 SQLite 直接拒绝，插件在数据库层面
       就没有改动存储数据的能力，不依赖调用方自觉。
    2. **不在数据目录里反复留下文件事件**。TodoDatabase 的 query()/tx() 是
       「一次操作开一条连接、用完关掉」；WAL 模式下关掉最后一条连接会创建再删除
       ``todo.db-wal`` / ``todo.db-shm``。网盘同步客户端按目录事件判断「文件变了」，
       每 2 秒一轮就成了 30 次/分钟的伪变动，看着就是这个插件一直在改数据、一直在上传。
       常驻连接把它摊平成「显示期间开一次，隐藏时随连接一起释放」。

    连接是跨线程使用的（刷新线程 + 页面 js_api 线程），所以用锁串行化访问，
    并用 ``isolation_level=None``（自动提交）确保不会长时间持有读快照 ——
    那会让主程序的 WAL 无法回收，反而把 -wal 越攒越大。
    """

    def __init__(self) -> None:
        self._conn: Optional[sqlite3.Connection] = None
        self._path: Optional[str] = None
        self._path_version: Optional[int] = None
        self._lock = threading.Lock()
        # 换库窗口标记：WebDAV 拉到新库做文件替换期间挂起，避免连接开着
        # 旧文件导致 Windows 上替换失败、或替换后读到旧 inode 的数据
        self._suspended = False

    # ------------------------------------------------------------------ #
    # 连接生命周期
    # ------------------------------------------------------------------ #

    @staticmethod
    def _storage_dir_version() -> int:
        """存储目录变更版本号；与 TodoDatabase.db_path 用同一套判定，配置不可用时按 0。"""
        try:
            from backend.storage.service import get_storage_dir_version
            return get_storage_dir_version()
        except Exception:
            return 0

    @staticmethod
    def _resolve_db_path() -> str:
        """当前数据文件路径。取不到时由 service 自己退回默认位置，保证总能连上。"""
        from backend.storage.service import get_data_file_or_fallback
        return str(get_data_file_or_fallback())

    def _open_locked(self) -> None:
        path = self._resolve_db_path()
        conn = sqlite3.connect(
            path,
            timeout=_reader_connect_timeout,
            isolation_level=None,      # 自动提交：每条 SELECT 各取一次新快照
            check_same_thread=False,   # 刷新线程与 js_api 线程共用（有锁串行化）
        )
        conn.row_factory = sqlite3.Row
        # 先用正常权限读一次库结构：万一 -wal/-shm 需要恢复，放在锁只读之前做完，
        # 之后就是纯粹的只读会话，不会再触发任何写动作。
        conn.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()
        conn.execute('PRAGMA query_only = ON')
        self._conn = conn
        self._path = path
        self._path_version = self._storage_dir_version()

    def _ensure_locked(self) -> None:
        """需要时建连；用户在运行期切换了存储目录（版本号或路径变了）就换一条连接。"""
        if self._suspended:
            # 换库窗口内禁止建连：此时主库文件可能正被替换，连接旧文件
            # 只会产生占用冲突或陈旧数据。抛错让取数走兜底查询，窗口
            # 结束（resume）后自动恢复
            raise RuntimeError('数据库文件正在同步替换，只读连接暂时挂起')
        if self._conn is not None:
            version = self._storage_dir_version()
            if version == self._path_version:
                return
            if self._resolve_db_path() == self._path:
                self._path_version = version
                return
            self.close_locked()
        self._open_locked()

    def close_locked(self) -> None:
        conn, self._conn = self._conn, None
        self._path, self._path_version = None, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            # 释放失败不值得拖垮插件：下一次取数会重新建连
            pass

    def close(self) -> None:
        """关闭常驻连接（插件隐藏时调用，把附属文件交还给主程序正常回收）。"""
        with self._lock:
            self.close_locked()

    def suspend(self) -> None:
        """进入换库窗口：关掉连接并暂停自动重连（供 WebDAV 安全换库调用）。

        与 close() 的区别是 close 之后下一次取数会立刻重建连接，而挂起
        状态下重建被拦住——否则文件替换还没完成连接又把旧文件握在手里。
        挂起期间取数会抛 RuntimeError，插件按「本轮取数失败」兜底处理。
        """
        with self._lock:
            self._suspended = True
            self.close_locked()

    def resume(self) -> None:
        """结束换库窗口，恢复正常取数（下一轮刷新自动重连新库）。"""
        with self._lock:
            self._suspended = False

    def warm_up(self) -> None:
        """显示插件时先把连接开好，让后续每轮推送只花一条 SELECT 的时间。"""
        with self._lock:
            self._ensure_locked()

    # ------------------------------------------------------------------ #
    # 取数
    # ------------------------------------------------------------------ #

    def read(self) -> Tuple[Dict[str, Any], Optional[Any]]:
        """返回「插件三项指标 + 语言设置原始值（没有该配置时 None）」。

        四条查询都走同一条常驻只读连接；插件的两项显示设置（优先级范围、
        倒计时区间）也在这里一并读出并解析成取数参数，设置页改完
        下一轮刷新（2 秒内）即生效。出异常时由调用方决定如何兜底，
        这里不吞异常，避免插件静默显示一份陈数据。
        """
        with self._lock:
            self._ensure_locked()
            raw_priorities = query_setting_value(self._conn, 'widget_priorities')
            raw_range = query_setting_value(self._conn, 'widget_countdown_range')
            priorities, range_end = _resolve_widget_scope(
                SettingCrudMixin._decode(raw_priorities) if raw_priorities is not None else None,
                SettingCrudMixin._decode(raw_range) if raw_range is not None else None,
                datetime.now(),
            )
            stats = query_desktop_widget_statistics(self._conn, priorities, range_end)
            language = query_setting_value(self._conn, 'language')
        return stats, SettingCrudMixin._decode(language) if language is not None else None


class DesktopWidgetBridge:
    """插件页面的 js_api：只暴露页面需要的动作，后端对象本身不外泄。"""

    def __init__(self, widget: 'DesktopWidget') -> None:
        self._widget = widget

    def get_widget_data(self) -> Dict[str, Any]:
        return self._widget.build_payload()

    def focus_main_window(self) -> None:
        self._widget.focus_main_window()

    def begin_widget_resize(self, corner: str) -> None:
        self._widget.begin_resize(corner)

    def update_widget_resize(self, size: int) -> None:
        self._widget.apply_resize(size)

    def end_widget_resize(self) -> None:
        self._widget.end_resize()


class DesktopWidget(LogManager):
    """桌面显示插件窗口（进程内单例）。"""

    _instance: Optional['DesktopWidget'] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        super().__init__()
        self.window: Any = None
        self.is_visible: bool = False
        self._stop_event = threading.Event()
        self._refresh_thread: Any = None
        # 首次显示前用 resize 校准过默认尺寸：pywebview 在 Windows 上创建无边框窗口时
        # 初始尺寸会被系统边框度量吃掉一块（如 360×120 实际得到 344×81），
        # 只有创建后的显式 resize 才能拿到精确尺寸；之后用户手动缩放的尺寸不干预
        self._size_calibrated = False
        # 缩放会话中记住按下的角，用于把对边的位置锚住（交给 pywebview 的 fix_point）
        self._resize_corner: Optional[str] = None
        # 取数只用常驻只读连接；TodoDatabase 留作兜底，需要时才惰性创建
        self._reader = ReadOnlyDatabaseReader()
        self._fallback_db: Any = None
        # 同一轮故障只记一次日志：插件每 2 秒刷一次，逐条写日志会让日志文件也一直在变
        self._warned_read_failure = False

    @classmethod
    def get_instance(cls) -> 'DesktopWidget':
        """取单例：首次调用才建窗口，之后只做显示/隐藏。"""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = DesktopWidget()
            return cls._instance

    # ------------------------------------------------------------------ #
    # 开关
    # ------------------------------------------------------------------ #

    def toggle(self) -> bool:
        """切换显示/隐藏，返回切换后的可见状态（前端按钮以此为准）。"""
        if self.is_visible:
            self.hide()
        else:
            self.show()
        return self.is_visible

    def show(self) -> None:
        self._ensure_window()
        if not self._size_calibrated:
            # 首次显示前校准默认尺寸（此后用户用四角缩放调整的尺寸不再干预）
            try:
                self.window.resize(widget_width, widget_height)
            except Exception as e:
                self.get_logger.warning(f"桌面插件默认尺寸校准失败（可忽略）: {e}")
            self._size_calibrated = True
        self.window.show()
        self.is_visible = True
        # 先把只读连接开好，第一帧就不用把建连时间算进刷新里
        self._reader.warm_up()
        self.push_stats()
        self._start_refresh()

    def hide(self) -> None:
        self._stop_refresh()
        self.is_visible = False
        self._resize_corner = None
        # 插件不显示就不必一直挂着连接和数据目录里的附属文件
        self._reader.close()
        if self.window:
            self.window.hide()

    def on_closing(self) -> bool:
        """无边框窗口没有关闭按钮，但 Alt+F4 仍会触发：只隐藏，不销毁。"""
        self.hide()
        return False

    # ------------------------------------------------------------------ #
    # 窗口
    # ------------------------------------------------------------------ #

    def _ensure_window(self) -> None:
        if self.window is not None:
            return

        import webview

        # hidden=True 创建、由 show()/hide() 控制可见：与快捷键面板同一套做法。
        # resizable=True 是四角缩放的前提（frameless 下窗口本身没有可拖的边框，
        # 缩放由页面四角手柄调用 resize 实现）；不传 x/y，位置交给系统默认。
        self.window = webview.create_window(
            app_name_widget,
            html=widget_html,
            js_api=DesktopWidgetBridge(self),
            width=widget_width,
            height=widget_height,
            min_size=(widget_min_size, int(widget_min_size / widget_aspect)),
            frameless=True,
            on_top=True,
            resizable=True,
            easy_drag=True,
            text_select=False,
            hidden=True,
        )
        self.window.events.closing += self.on_closing

        # 插件窗不该出现在任务栏（与快捷键窗口同理，各平台自行实现，默认空实现）
        try:
            self.service.hide_taskbar_icon(self.window)
        except Exception as e:
            self.get_logger.warning(f"桌面插件窗口取消任务栏图标失败（可忽略）: {e}")

    # ------------------------------------------------------------------ #
    # 取数与推送
    # ------------------------------------------------------------------ #

    def build_payload(self) -> Dict[str, Any]:
        """组装插件页面所需的数据。

        全部输入都是「库里已有的信息 + 系统时间」：指标来自一条只读 SELECT，
        倒计时是 next_due 减 datetime.now()，不会写库、也不会改动任何存储文件。

        优先级顺序按用户要求：只要有逾期任务就先显示「已逾期x个」，此时不再给倒计时；
        没有逾期才看下一个未到期的任务；两者都没有则是「无事发生」的宽松态（绿色）。
        """
        stats, language = self._read_snapshot()
        now = datetime.now()
        next_due = _parse_due(stats.get('next_due'))

        payload: Dict[str, Any] = {
            'unfinished': stats['unfinished'],
            'over_due': stats['over_due'],
            'labels': _widget_labels[_language_code(language)],
            'levels': _build_color_levels(),
        }

        if stats['over_due'] > 0:
            payload['mode'] = 'overdue'
            payload['remaining'] = 0
        elif next_due and next_due > now:
            payload['mode'] = 'countdown'
            payload['remaining'] = int((next_due - now).total_seconds())
        else:
            payload['mode'] = 'idle'
            payload['remaining'] = 0

        return payload

    def push_stats(self) -> None:
        """把最新数据推给页面。失败只记日志（同类只记一次），不能让刷新线程崩掉。"""
        try:
            payload = self.build_payload()
            self.window.evaluate_js(
                f"window.__widgetRender && window.__widgetRender({json.dumps(payload)});")
        except Exception as e:
            self._warn_read_failure(f"桌面插件刷新数据失败: {e}")

    # ------------------------------------------------------------------ #
    # 只读取数
    # ------------------------------------------------------------------ #

    def _read_snapshot(self) -> Tuple[Dict[str, Any], str]:
        """读插件需要的三项指标与界面语言，返回 ``(指标, 语言键)``。

        正常路径走常驻只读连接。连不上时（目录权限、库被别的进程独占等）退回
        TodoDatabase 的只读查询：先保证插件还能显示，再用一次性告警把问题暴露出来。
        """
        try:
            stats, language = self._reader.read()
            self._warned_read_failure = False
            return stats, _language_code(language)
        except Exception as e:
            self._warn_read_failure(f"桌面插件只读连接取数失败，本轮改用普通只读查询: {e}")

        try:
            db = self._get_fallback_db()
            priorities, range_end = _resolve_widget_scope(
                db.get_setting('widget_priorities'),
                db.get_setting('widget_countdown_range'),
                datetime.now(),
            )
            stats = db.get_desktop_widget_statistics(priorities, range_end)
            language = _language_code(db.get_setting('language', 'zh'))
            return stats, language
        except Exception as e:
            self._warn_read_failure(f"桌面插件取数失败，本轮按无任务显示: {e}")
            return {'unfinished': 0, 'over_due': 0, 'next_due': None}, 'zh'

    def _get_fallback_db(self) -> Any:
        """兜底用的数据库对象：只在只读连接不可用时才创建。

        之所以不放在 __init__：构造 TodoDatabase 会顺带跑一遍建表 / 补列 / 孤儿清理，
        插件本来不需要这些，也不该为了「看一眼数字」去碰写路径。
        """
        if self._fallback_db is None:
            from backend.database.todo_database import TodoDatabase
            self._fallback_db = TodoDatabase()
        return self._fallback_db

    def _warn_read_failure(self, message: str) -> None:
        """同类告警只记一次，直到下一轮成功取数才重新开放计数。

        刷新线程每 2 秒跑一次，逐条写日志会让日志文件也一直在变 ——
        那正是网盘「一直在上传」的另一种表现，不能一边修一边制造。
        """
        if self._warned_read_failure:
            return
        self._warned_read_failure = True
        self.get_logger.warning(message)

    def reload_data(self) -> None:
        """放掉手里的只读连接与兜底实例，下一轮取数重新连到当前生效的库。

        切换存储目录时版本号会变，`ReadOnlyDatabaseReader` 会自己重开；需要主动放掉的是
        「目录没变、用户在应用外替换了 todo.db 后点重新加载」这一种 —— 版本号不动，
        常驻连接会一直显示旧文件里的数字。TodoDatabase 本身不持有长连接，丢掉引用即可。
        """
        self._reader.close()
        self._fallback_db = None
        self._warned_read_failure = False

    # ------------------------------------------------------------------ #
    # 交互：唤起主窗口 / 等比缩放
    # ------------------------------------------------------------------ #

    def focus_main_window(self) -> None:
        """双击插件：把主窗口带到前台。

        实际动作放到后台线程执行：js_api 调用本身跑在 WebView2 的 UI 线程上，
        而窗口唤起同样要在 UI 线程完成。同步做会让 UI 线程卡在这次调用里，
        主窗口刚被激活就发出的取数请求排队等不到线程，表现就是
        「主界面显示正在刷新，然后未响应」。与 smart_task.notify_main_window
        用的是同一套规避做法。
        """
        threading.Thread(target=self._bring_main_window_forward, daemon=True).start()

    def _bring_main_window_forward(self) -> None:
        # 优先用平台层现成的唤起实现（Windows/macOS）：它只调 user32/系统 API，
        # 不经过 pywebview 的 events.wait 与 Control.Invoke，因此既不会卡 UI 线程，
        # 也不存在跨线程访问控件的问题，并且自带「显示 + 还原 + 置顶一次」的完整动作。
        try:
            activate = getattr(self.service, 'activate_existing_window', None)
            if callable(activate) and activate():
                return
        except Exception as e:
            self.get_logger.warning(f"平台层唤起主窗口失败，改用 pywebview 接口: {e}")

        self._bring_main_window_forward_via_webview()

    def _bring_main_window_forward_via_webview(self) -> None:
        """兜底路径（如 Linux 没有平台层实现时）：只做能安全跨线程调用的动作。

        这里刻意不碰 on_top —— pywebview 的 set_on_top 直接写 Form.TopMost，
        没有做 UI 线程转发，从本线程赋值属于跨线程访问控件。
        """
        try:
            import backend.globals

            main_window = backend.globals.window
            if not main_window:
                return

            main_window.show()
            main_window.restore()
        except Exception as e:
            self.get_logger.warning(f"从桌面插件唤起主窗口失败: {e}")

    def begin_resize(self, corner: str) -> None:
        self._resize_corner = corner if corner in ('tl', 'tr', 'bl', 'br') else None

    def apply_resize(self, size: Any) -> None:
        """按目标宽度等比改变窗口（高度 = 宽度 / widget_aspect），并按按下的角锚定对边。"""
        corner = self._resize_corner
        if not corner:
            return

        try:
            target_width = int(max(widget_min_size, min(widget_max_size, int(size))))
        except (TypeError, ValueError):
            return
        target_height = max(1, int(round(target_width / widget_aspect)))

        from webview.window import FixPoint

        # winforms 只认 EAST/SOUTH 两个位：置位表示那条边不动
        fix_point_by_corner: Dict[str, Any] = {
            'br': FixPoint.NORTH | FixPoint.WEST,   # 左上角不动
            'bl': FixPoint.EAST,                    # 右上角不动
            'tr': FixPoint.SOUTH,                   # 左下角不动
            'tl': FixPoint.EAST | FixPoint.SOUTH,   # 右下角不动
        }

        try:
            self.window.resize(target_width, target_height,
                               fix_point=fix_point_by_corner[corner])
        except Exception as e:
            self.get_logger.warning(f"桌面插件缩放失败: {e}")

    def end_resize(self) -> None:
        self._resize_corner = None

    # ------------------------------------------------------------------ #
    # 刷新线程
    # ------------------------------------------------------------------ #

    def _start_refresh(self) -> None:
        if self._refresh_thread and self._refresh_thread.is_alive():
            return
        self._stop_event.clear()
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

    def _stop_refresh(self) -> None:
        self._stop_event.set()
        thread = self._refresh_thread
        self._refresh_thread = None
        # 线程最多阻塞一个刷新周期 + 一次查询，给 3 秒足够退出；超时也不强杀
        if thread and thread.is_alive():
            thread.join(timeout=3.0)

    def _refresh_loop(self) -> None:
        while not self._stop_event.wait(refresh_interval):
            self.push_stats()


def reset_widget_readers() -> None:
    """让桌面显示插件放掉数据库连接，下一轮重新取数。

    由「切换存储目录 / 重新加载数据」这两个 API 调用（见
    backend/api/mixins/datafile_api_mixin.py）：常驻只读连接是这次改动换来的新副作用，
    用户在应用外替换了 todo.db 时，版本号与路径都没变，只有主动放掉才会重连。
    插件从未创建过时直接返回，不实例化、不导入，移动端零影响。
    """
    widget = DesktopWidget._instance
    if widget is not None:
        widget.reload_data()
