"""Bounded, unauthenticated checks of one server-owned website route.

Never accepts a browser/model supplied URL, follows redirects, executes website
JavaScript or sends console cookies. Response bodies stay local to the checks.
"""
import hashlib
import http.client
import re
from datetime import datetime, timezone

import site_files

PROMPT = "对我创建的当前网站执行只读漏洞测试，结合实际 HTTP 检查和本站源码给出有证据的安全报告。"
SENSITIVE_PATHS = ('.env', '.git/config', 'site.json', 'runtime.json',
                   'backend/app.py', 'backend/server.js', 'database.sqlite3', 'backup.sql')
BODY_LIMIT = 65536


def target(workspace, slug, preview_id, public_url=''):
    if site_files.read_metadata(workspace).get('format') != 'files':
        raise ValueError('请先打开网站编辑器，完成本站文件初始化后再测试')
    if slug:
        if not re.fullmatch(r'[A-Za-z0-9_-]{3,32}', slug):
            raise ValueError('本站发布路径无效')
        path = f'/pages/{slug}/'
    else:
        if not re.fullmatch(r'[A-Za-z0-9_-]{32}', preview_id):
            raise ValueError('本站预览地址无效')
        path = f'/preview/{preview_id}/'
    return {'name': site_files.read_metadata(workspace).get('siteName') or '我的网站',
            'path': path, 'url': public_url or path, 'published': bool(slug)}


def _request(port, path):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        conn.request('GET', path, headers={'Cache-Control': 'no-cache', 'Connection': 'close'})
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read(BODY_LIMIT + 1)
    finally:
        conn.close()


def inspect(workspace, target, port, *, tls_ready=None, progress=None):
    prefix = target['path']
    # Defense in depth: callers must not turn the loopback transport into a
    # generic console/private-network fetcher, even if future UI inputs change.
    if not re.fullmatch(r'/(?:pages/[A-Za-z0-9_-]{3,32}|preview/[A-Za-z0-9_-]{32})/', prefix):
        raise ValueError('测试只能访问本站发布或预览路径')
    checks = []
    def add(name, status, detail):
        checks.append({'name': name, 'status': status, 'detail': detail})
        if progress:
            progress({'id': 'security-baseline', 'kind': 'tool', 'label': '检查本站 HTTP 响应',
                      'detail': f'已检查 {len(checks)} 项 · {prefix}', 'status': 'running',
                      'security': {'target': target, 'checks': list(checks)}})

    home = None
    response_headers = {}
    try:
        status, headers, home = _request(port, prefix)
        response_headers = {key: headers[key][:2048] for key in (
            'content-security-policy', 'x-content-type-options', 'referrer-policy',
            'x-frame-options', 'strict-transport-security') if key in headers}
        add('本站首页', 'pass' if status == 200 else 'warn', f'GET {prefix} → HTTP {status}；不跟随重定向')
        if status == 200:
            for key, label in (('content-security-policy', '内容安全策略'),
                               ('x-content-type-options', '内容类型保护'), ('referrer-policy', '来源信息策略')):
                valid = bool(headers.get(key)) if key != 'x-content-type-options' else headers.get(key, '').lower() == 'nosniff'
                add(label, 'pass' if valid else 'warn',
                    f'{key} 已设置' if valid else f'未发现有效 {key}，需结合站点功能评估；不等同于已确认漏洞')
            framed = bool(headers.get('x-frame-options')) or 'frame-ancestors' in headers.get('content-security-policy', '').lower()
            add('页面嵌入保护', 'pass' if framed else 'warn',
                '存在 X-Frame-Options 或 CSP frame-ancestors' if framed else '未发现页面嵌入限制，需复核是否存在点击劫持风险')
    except (OSError, http.client.HTTPException):
        add('本站首页', 'warn', '请求失败或超时，无法验证；没有将网络失败视为通过')

    for name in SENSITIVE_PATHS:
        try:
            status, _, body = _request(port, prefix + name)
            if status in (401, 403, 404, 410):
                state, detail = 'pass', f'HTTP {status}，此次未取得文件内容'
            elif status == 200:
                local = None
                try:
                    path = site_files.confined_path(workspace, name)
                    if path.is_file():
                        with path.open('rb') as source:
                            local = source.read(BODY_LIMIT + 1)
                except (OSError, ValueError):
                    pass
                if local and body == local:
                    state, detail = 'fail', 'HTTP 200，响应内容与本站文件一致，确认可公开读取（内容已隐藏）'
                elif home is not None and body == home:
                    state, detail = 'warn', 'HTTP 200，但与首页响应相同，可能为兜底页，不能认定文件泄露'
                else:
                    state, detail = 'warn', 'HTTP 200，无法确认是否为文件内容，需复核；响应内容未保存'
            else:
                state, detail = 'warn', f'HTTP {status}，未确认是否暴露；未跟随重定向'
            add(f'敏感文件 /{name}', state, detail)
        except (OSError, http.client.HTTPException):
            add(f'敏感文件 /{name}', 'warn', '请求失败或超时，未验证')
    add('独立域名证书', 'skip' if tls_ready is None else ('pass' if tls_ready else 'warn'),
        '未配置独立域名，当前检查不验证公网 TLS' if tls_ready is None else
        ('本站域名最近的实际 TLS 握手已通过主机名、信任链和有效期验证' if tls_ready else '本站域名 TLS 握手未通过，需检查证书和 HTTPS 配置'))
    return {'target': target, 'checks': checks, 'checkedAt': datetime.now(timezone.utc).isoformat(),
            'responseHeaders': response_headers,
            'sourceFingerprint': hashlib.sha256(str(sorted(site_files.file_manifest(workspace).items())).encode()).hexdigest(),
            'scope': 'HTTP 结果来自本站应用发布/预览路径，不代表公网代理响应头；仅固定路径 GET，不执行网页脚本或写入请求。动态接口的鉴权、注入和业务逻辑由模型审查本站源码，未做登录或数据变更验证。'}
