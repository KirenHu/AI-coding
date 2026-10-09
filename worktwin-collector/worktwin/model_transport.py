"""Verified HTTPS and credential-free diagnostics for model providers."""
from functools import lru_cache
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import certifi


class ModelConnectionError(RuntimeError):
    """Only predefined, safe messages may be displayed to the user."""


@lru_cache(maxsize=1)
def model_ssl_context():
    # Keep system/enterprise trust and add a bundled public CA store. Frozen
    # Python cannot rely on the build machine's OpenSSL certificate paths.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


def model_urlopen(request, timeout=120):
    try:
        return urlopen(request, timeout=timeout, context=model_ssl_context())
    except HTTPError as exc:
        code = exc.code
        exc.close()
        messages = {
            400: '模型服务拒绝请求，请检查模型名称和接口兼容性',
            401: 'API Key 无效或已失效，请在服务商后台重新创建并填写',
            402: '模型账户余额不足，请在服务商后台检查 API 余额',
            403: '服务商拒绝访问，请检查 API Key 的权限及地区限制',
            404: '模型或接口不存在，请检查服务地址和模型名称',
            429: '模型服务请求过于频繁或额度已满，请稍后重试或检查额度',
        }
        message = messages.get(code, '模型服务暂时不可用，请稍后重试' if code >= 500 else '模型服务拒绝请求，请检查服务商设置')
        raise ModelConnectionError(f'{message}（HTTP {code}）') from None
    except (ssl.SSLError, URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        if isinstance(reason, ssl.SSLError):
            message = 'HTTPS 证书验证失败，请检查系统时间、代理或企业网络证书'
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            message = '连接模型服务超时，请检查网络或代理后重试'
        else:
            message = '无法连接模型服务，请检查服务地址、网络或代理'
        # Never expose URLs, response bodies, headers, keys or proxy passwords.
        raise ModelConnectionError(message) from None


def chat_payload(url, model, messages, max_tokens, temperature=0.1):
    payload = {'model': model, 'messages': messages, 'max_tokens': max_tokens,
               'temperature': temperature, 'stream': False}
    if urlparse(url).hostname == 'api.deepseek.com':
        # DeepSeek now defaults to reasoning. Our structured extraction and
        # connection probe need a final answer within a bounded output budget.
        payload['thinking'] = {'type': 'disabled'}
    return payload


MODEL_TEST_TOKENS = 64
