"""Read-only local diagnostics. Does not load or print credential files."""
import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def diagnose(data=None):
    root = Path(data or os.environ.get('SKILL_COLLECT_DATA', PROJECT / 'data')).resolve()
    checks = []
    checks.append({'name': 'Python', 'required': True, 'ok': sys.version_info >= (3, 11), 'detail': platform.python_version()})
    for dependency in ('fastapi', 'uvicorn', 'httpx', 'pydantic', 'python-multipart', 'cryptography'):
        try:
            checks.append({'name': dependency, 'required': True, 'ok': True, 'detail': importlib.metadata.version(dependency)})
        except importlib.metadata.PackageNotFoundError:
            checks.append({'name': dependency, 'required': True, 'ok': False, 'detail': '未安装；请运行 setup'})
    node = shutil.which('node')
    version = None
    if node:
        try:
            version = subprocess.run([node, '--version'], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
    major, minor = (0, 0)
    try:
        major, minor = map(int, (version or '').lstrip('v').split('.')[:2])
    except ValueError:
        pass
    checks.append({'name': 'Node.js', 'required': True, 'ok': (major == 20 and minor >= 19) or (major == 22 and minor >= 12) or major >= 23,
                   'detail': version or '未找到；https://nodejs.org/'})
    checks.append({'name': '前端构建', 'required': True, 'ok': (PROJECT / 'frontend/dist/index.html').is_file(),
                   'detail': 'frontend/dist/index.html'})
    for binary in ('ffmpeg', 'ffprobe'):
        checks.append({'name': binary, 'required': False, 'ok': bool(shutil.which(binary)),
                       'detail': '已找到' if shutil.which(binary) else '缺失，媒体处理受限：https://ffmpeg.org/download.html'})
    checks.append({'name':'Git公开仓库降级读取','required':False,'ok':bool(shutil.which('git')),'detail':'已找到Git' if shutil.which('git') else '未安装；匿名API限流时需安装Git或配置GitHub Token：https://git-scm.com/downloads'})
    db_path = root / 'app.sqlite3'
    db_ok, detail = False, '未初始化；先运行 setup'
    if db_path.is_file():
        try:
            with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
                db_ok = db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                detail = 'SQLite 完整性正常' if db_ok else 'SQLite 完整性异常'
        except sqlite3.DatabaseError:
            detail = 'SQLite 无法读取'
    checks.append({'name': '资料库', 'required': True, 'ok': db_ok, 'detail': detail})
    return {'ok': all(c['ok'] for c in checks if c['required']), 'platform': platform.system(), 'data_directory': str(root),
            'checks': checks, 'notice': '诊断只检查本地环境；不证明模型、GitHub、飞书或视频来源已真实联通。'}


def port_available(port):
    # A recently stopped server may leave TIME_WAIT sockets. Check listeners,
    # then use the same address reuse semantics as a new local server on POSIX.
    try:
        with socket.socket() as probe:
            probe.settimeout(.2)
            if probe.connect_ex(('127.0.0.1',port))==0:return False
        with socket.socket() as sock:
            if os.name!='nt':sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            elif hasattr(socket,'SO_EXCLUSIVEADDRUSE'):sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            sock.bind(('127.0.0.1',port))
        return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description='只读本地诊断，不读取凭证、不调用外部服务。')
    parser.add_argument('--data')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--check-port', type=int)
    args = parser.parse_args()
    if args.check_port is not None:
        if not 1024 <= args.check_port <= 65535:
            print('端口必须在 1024—65535 之间', file=sys.stderr)
            return 1
        if not port_available(args.check_port):
            print('端口已占用；不会结束其他进程', file=sys.stderr)
            return 1
        return 0
    report = diagnose(args.data)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in report['checks']:
            mark = '通过' if check['ok'] else '失败' if check['required'] else '受限'
            print(f'[{mark}] {check["name"]}: {check["detail"]}')
        print(report['notice'])
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
