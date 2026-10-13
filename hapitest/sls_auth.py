"""SLS 0.6.0 HMAC-SHA1 请求签名（仅标准库，支持 RAM / STS）。

签名协议来源：Alibaba Cloud SLS request-signatures 文档及官方 Python SDK AuthV1。
查询使用 GetLogsV2 POST /logstores/{logstore}/logs，响应选择 gzip。
"""
import base64
import hashlib
import hmac
from email.utils import formatdate
from urllib.parse import parse_qsl, urlsplit


def sign(method, url, headers, body, auth, resolver):
    def value(text):
        return resolver.resolve_text(text) if resolver else str(text or '')

    access_id, secret, token = value(auth.username), value(auth.password), value(auth.token)
    if not access_id or not secret or '${' in access_id or '${' in secret:
        raise ValueError('SLS 查询缺少 AccessKey ID / Secret')
    # 统一签名 header 的大小写，避免重复头和签名不一致。
    for key in list(headers):
        if key.lower().startswith(('x-log-', 'x-acs-')):
            headers[key.lower()] = headers.pop(key)
    headers['x-log-apiversion'] = '0.6.0'
    headers['x-log-bodyrawsize'] = str(len(body or b''))
    headers['x-log-signaturemethod'] = 'hmac-sha1'
    if token:
        headers['x-acs-security-token'] = token
    headers.setdefault('Date', formatdate(usegmt=True))
    digest = hashlib.md5(body).hexdigest().upper() if body else ''
    if digest:
        headers['Content-MD5'] = digest
    content_type = next((v for k, v in headers.items() if k.lower() == 'content-type'), '')
    canonical = ''.join('%s:%s\n' % (key, headers[key]) for key in sorted(headers)
                        if key.startswith(('x-log-', 'x-acs-')) and not key.startswith('x-log-meta-'))
    parts = urlsplit(url)
    params = sorted(parse_qsl(parts.query, keep_blank_values=True))
    resource = parts.path + ('?' + '&'.join('%s=%s' % pair for pair in params) if params else '')
    signed_text = '\n'.join([method, digest, content_type, headers['Date'], canonical + resource])
    signature = base64.b64encode(hmac.new(secret.encode(), signed_text.encode(), hashlib.sha1).digest()).decode()
    headers['Authorization'] = 'LOG %s:%s' % (access_id, signature)

