"""Offline purpose-specific request policy, not a live login claim."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('url,kind,allowed', [
    ('https://www.douyin.com/', 'document', True),
    ('https://www.douyin.com/user/self', 'document', True),
    ('https://www.douyin.com/aweme/v1/web/user/profile/self/', 'xhr', True),
    ('https://sso.douyin.com/check_qrconnect/', 'xhr', True),
    ('https://sso.douyin.com/get_qrcode/', 'fetch', True),
    ('https://verify.snssdk.com/captcha/get', 'fetch', True),
    ('https://p6-captcha.byteimg.com/captcha-image.png', 'image', True),
    ('https://static.douyin.com/login.js', 'script', True),
    ('https://www.douyin.com/passport/verify/', 'document', True),
    ('https://v3.douyinvod.com/video/synthetic', 'fetch', False),
    ('https://v5.douyincdn.com/video/synthetic', 'xhr', False),
    ('https://v26.zjcdn.com/video/synthetic', 'fetch', False),
    ('https://www.douyin.com/aweme/v1/web/tab/feed/', 'xhr', False),
    ('https://www.douyin.com/aweme/v1/web/related/aweme/', 'fetch', False),
    ('https://www.douyin.com/aweme/v1/web/comment/list/', 'fetch', False),
    ('https://ordinary-cdn.invalid/video.mp4', 'media', False),
    ('https://douyinvod.com.attacker.invalid/login.js', 'script', True),
    ('not-a-url', 'fetch', False),
], ids=['home', 'self', 'profile', 'qr-poll', 'qr-issue', 'captcha-api', 'captcha-image',
        'login-script', 'verification-page', 'vod-fetch', 'cdn-xhr', 'zjcdn-fetch',
        'feed', 'related', 'comments', 'media', 'exact-domain-boundary', 'malformed'])
def test_login_request_policy_preserves_authentication_resources_and_avoids_background_video(url, kind, allowed):
    script = """import {loginResourceAllowed} from './frontend/scripts/douyin-login.mjs';
console.log(JSON.stringify({allowed:loginResourceAllowed(process.argv[1],process.argv[2])}));"""
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script, url, kind],
        cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout) == {'allowed': allowed}
