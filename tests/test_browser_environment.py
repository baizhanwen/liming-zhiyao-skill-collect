"""The standalone product renderer receives runtimes, never assistant/provider secrets."""
from backend.browser_capture import _renderer_env


def test_renderer_environment_has_no_codex_provider_credentials_or_network_overrides(monkeypatch):
    secrets={'CODEX_HOME':'SYNTHETIC_ASSISTANT_SETTINGS','CODEX_THREAD_ID':'SYNTHETIC_THREAD',
        'MINIMAX_API_KEY':'SYNTHETIC_MODEL_KEY','GITHUB_TOKEN':'SYNTHETIC_GITHUB_KEY',
        'HTTP_PROXY':'http://SYNTHETIC_PROXY','HTTPS_PROXY':'http://SYNTHETIC_PROXY',
        'NODE_OPTIONS':'--require=SYNTHETIC_HOOK','SSLKEYLOGFILE':'SYNTHETIC_TLS_LOG'}
    for name,value in secrets.items():monkeypatch.setenv(name,value)
    monkeypatch.setenv('PATH','SYNTHETIC_RUNTIME_PATH')
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH','SYNTHETIC_PROJECT_BROWSER_CACHE')
    result=_renderer_env()
    assert not (set(secrets)&set(result))
    assert not any(value in result.values() for value in secrets.values())
    assert result['PATH']=='SYNTHETIC_RUNTIME_PATH'
    assert result['PLAYWRIGHT_BROWSERS_PATH']=='SYNTHETIC_PROJECT_BROWSER_CACHE'
