"""Data and notifications connect directly; only model clients receive proxy settings."""
import os

DEFAULT_PROXY_URL = "http://127.0.0.1:7890"
PROXY_KEYS = ('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY')


def model_proxy_url():
    return os.environ.get('GOLD_TRADING_HTTP_PROXY',DEFAULT_PROXY_URL).strip()


def apply_proxy() -> str:
    """Keep the monitor process direct even when its parent shell has proxy variables."""
    for key in PROXY_KEYS:os.environ.pop(key,None)
    return model_proxy_url()


def model_environment(source=None):
    """Private child environment; never mutate parent-process routing."""
    env=dict(os.environ if source is None else source)
    for key in PROXY_KEYS:env.pop(key,None)
    proxy=env.get('GOLD_TRADING_HTTP_PROXY',DEFAULT_PROXY_URL).strip()
    if proxy:
        env['http_proxy']=proxy;env['https_proxy']=proxy
    return env
