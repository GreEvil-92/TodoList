#!/usr/bin/env python3
"""安卓打包配置自检（不联网、不需要 Linux，可在 Windows 上直接跑）

它检查的是「p4a/buildozer 会在构建开始几分钟后才报错」的那类问题：
引用文件缺失、绝对路径写死、给 recipe 包乱写版本号、资源扩展名漏配。
用法：python scripts/check_android_config.py
"""
from __future__ import annotations

import configparser
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / 'scripts' / 'config' / 'buildozer.spec'

# 有 recipe 的包：版本由 p4a 的 recipe 决定，写版本号不匹配会直接中断构建
RECIPE_PACKAGES = {
    'python3', 'hostpython3', 'kivy', 'pyjnius', 'android', 'lxml',
    'openssl', 'sqlite3', 'libffi', 'sdl2', 'pillow', 'numpy',
}

errors: list[str] = []
warnings: list[str] = []
notes: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def parse_spec() -> configparser.ConfigParser:
    if not SPEC.exists():
        err(f'找不到配置文件：{SPEC}')
        return configparser.ConfigParser()
    cp = configparser.ConfigParser(interpolation=None)
    # buildozer 的 spec 允许重复键/裸值，这里只做宽松解析用于自检
    cp.read(SPEC, encoding='utf-8')
    return cp


def main() -> int:
    cp = parse_spec()
    if 'app' not in cp:
        err('spec 缺少 [app] 段')
        report()
        return 1

    app = cp['app']

    # 1. 必填项
    for key in ('title', 'package.name', 'package.domain', 'source.dir', 'requirements'):
        if not app.get(key, '').strip():
            err(f'缺少必填项 {key}')

    pkg = app.get('package.name', '').strip()
    if pkg and not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]*', pkg):
        err(f'package.name = {pkg!r} 不合法（只能字母数字下划线，且不能有短横线）')
    domain = app.get('package.domain', '').strip()
    if domain and '.' not in domain:
        warn(f'package.domain = {domain!r} 建议使用反向域名，如 com.yourname')

    # 2. 引用文件是否存在（相对项目根目录）
    for key in ('icon.filename', 'presplash.filename',
                'android.extra_manifest_application_arguments'):
        value = app.get(key, '').strip()
        if not value:
            continue
        path = (ROOT / value).resolve()
        if not path.exists():
            err(f'{key} 指向的文件不存在：{value}')
        else:
            notes.append(f'{key} = {value} ✓')

    # 3. 绝对路径检查：换机器必炸
    for key in ('p4a.source_dir', 'p4a.local_recipes', 'android.python3',
                'android.sdk_path', 'android.ndk_path'):
        value = app.get(key, '').strip()
        if value and (value.startswith('/') or re.match(r'^[A-Za-z]:[\\/]', value)):
            err(f'{key} 是绝对路径（{value}），换构建机必然失败，请注释掉或改成相对路径')

    # 4. requirements 检查
    reqs = [r.strip() for r in app.get('requirements', '').split(',') if r.strip()]
    for item in reqs:
        name, _, version = item.partition('==')
        name = name.strip().lower()
        if version and name in RECIPE_PACKAGES:
            err(f'requirements 里给 recipe 包 {name} 指定了版本 {version}，'
                f'p4a 会因版本不匹配中断构建，请去掉 "=={version}"')
    for need in ('pywebview', 'pyjnius', 'android'):
        if need not in [r.partition('==')[0].strip().lower() for r in reqs]:
            err(f'requirements 缺少 {need}（pywebview 安卓后端必需）')
    notes.append(f'requirements 共 {len(reqs)} 项：{", ".join(reqs)}')

    # 5. jar 引用
    jar = app.get('android.add_jars', '').strip()
    if jar and not jar.startswith('#'):
        for one in [j.strip() for j in jar.split(',') if j.strip()]:
            if '*' not in one and not (ROOT / one).exists():
                err(f'android.add_jars 指向的文件不存在：{one}')

    # 6. 资源扩展名是否覆盖前端实际用到的类型
    exts = {e.strip().lower() for e in app.get('source.include_exts', '').split(',') if e.strip()}
    frontend = ROOT / 'frontend'
    if frontend.is_dir():
        actual = {p.suffix.lstrip('.').lower() for p in frontend.rglob('*') if p.is_file()}
        missing = sorted(a for a in actual if a and a not in exts)
        if missing:
            err(f'source.include_exts 漏了前端用到的扩展名：{", ".join(missing)}（打包后这些文件会缺失）')
        else:
            notes.append(f'前端资源类型全部覆盖：{", ".join(sorted(actual))}')

    # 7. p4a 入口文件
    main_py = ROOT / 'main.py'
    if not main_py.exists():
        err('缺少 main.py（p4a 的默认入口）')
    else:
        head = main_py.read_text(encoding='utf-8', errors='ignore')
        for bad in ('import pystray', 'import pynput', 'import win32api', 'import pywin32'):
            if bad in head:
                err(f'main.py 顶层出现桌面专属导入（{bad}），安卓端会启动即崩溃')

    # 8. 关键源码是否具备安卓分支
    android_impl = ROOT / 'backend' / 'platforms' / 'impl' / 'mobile' / 'android_impl.py'
    factory = ROOT / 'backend' / 'platforms' / 'core' / 'factory.py'
    if not android_impl.exists():
        err('缺少 backend/platforms/impl/mobile/android_impl.py')
    if factory.exists():
        text = factory.read_text(encoding='utf-8', errors='ignore')
        if 'getandroidapilevel' not in text:
            warn('factory.py 未检测到安卓平台识别逻辑')

    # 9. 构建环境提示（Windows 上必然跑不了 buildozer）
    if os.name == 'nt':
        notes.append('当前是 Windows：buildozer 无法在此运行，需用 WSL2/Docker/Linux 构建')

    report()
    return 0 if not errors else 1


def report() -> None:
    print('=' * 62)
    print('安卓打包配置自检结果')
    print('=' * 62)
    for n in notes:
        print(f'  · {n}')
    if warnings:
        print('\n警告：')
        for w in warnings:
            print(f'  ! {w}')
    if errors:
        print('\n错误（会导致构建失败）：')
        for e in errors:
            print(f'  × {e}')
    print()
    print(f'结论：{len(errors)} 个错误，{len(warnings)} 个警告')
    print('=' * 62)


if __name__ == '__main__':
    sys.exit(main())
