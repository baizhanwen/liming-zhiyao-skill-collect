"""Install the lockfile's official Chromium runtime, inside the user's cache.

This does not install OS packages, change global Node/Python, inspect any browser
profile, or visit a video site. Browser availability is not a retrieval test.
"""
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
OFFICIAL_DOCS = 'https://playwright.dev/docs/browsers'


def install_browser(project=PROJECT):
    node = shutil.which('node')
    cli = Path(project) / 'frontend/node_modules/@playwright/test/cli.js'
    runtime = Path(project) / 'frontend/scripts/capture-douyin.mjs'
    if not node or not cli.is_file() or not runtime.is_file():
        print('缺少项目锁定的 Playwright；请先运行 setup 安装 Node.js 与前端依赖。', file=sys.stderr)
        return 1
    env = os.environ.copy()
    # The installer uses Playwright's bundled, official download locations.
    # Remove mirror overrides only in this child; leave user/global settings
    # untouched. PLAYWRIGHT_BROWSERS_PATH remains a supported cache location.
    for name in ('PLAYWRIGHT_DOWNLOAD_HOST', 'PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST',
                 'PLAYWRIGHT_FFMPEG_DOWNLOAD_HOST'):
        env.pop(name, None)
    print('安装项目锁定的官方 Chromium（首次需联网下载；不会使用现有浏览器登录）。', flush=True)
    try:
        result = subprocess.run([node, str(cli), 'install', 'chromium'], cwd=project,
                                env=env, timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode:
        print('Chromium 安装失败；请检查到官方 Playwright 下载源的网络后重跑 setup。\n'
              '不会自动安装系统库或提权。Linux 系统库要求见：' + OFFICIAL_DOCS,
              file=sys.stderr)
        return 1
    try:
        checked = subprocess.run([node, str(runtime), '--check-runtime'], cwd=project, env=env,
                                 capture_output=True, text=True, timeout=10, check=False)
        report = json.loads(checked.stdout)
        ready = checked.returncode == 0 and isinstance(report, dict) and report.get('ok') is True and report.get('browser_installed') is True
    except (OSError, subprocess.SubprocessError, ValueError):
        ready = False
    if not ready:
        print('Chromium 安装命令已结束，但项目运行时未通过文件检查；请核对完整源码并重跑 setup。', file=sys.stderr)
        return 1
    print('Chromium 安装命令完成（已安装版本复用缓存）；仍需分别验收原片获取、转写与模型分析。')
    return 0


if __name__ == '__main__':
    raise SystemExit(install_browser())
