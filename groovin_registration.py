"""Bounded, same-origin registration bridge to the shared SDSZ account service.

No local OTP store or user writes: the upstream owns campus eligibility, email
delivery, verification and registration. Mutation requests are never retried.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
from urllib.parse import urlsplit

GRADES = ('初一', '初二', '初三', '高一', '高二', '高三', '毕业年级')
CLASSES = tuple(f'{i}班' for i in range(1, 25))
FIELDS = {
    'campus': ('campusId',),
    'send-otp': ('campusId', 'email'),
    'register': ('campusId', 'name', 'realName', 'email', 'grade', 'classGroup', 'password', 'code'),
}


def validate(operation, payload):
    if operation not in FIELDS or not isinstance(payload, dict):
        raise ValueError('注册请求格式无效')
    limits = {'campusId': 64, 'name': 50, 'realName': 80, 'email': 254,
              'grade': 20, 'classGroup': 20, 'password': 128, 'code': 6}
    data = {}
    for field in FIELDS[operation]:
        value = payload.get(field, '')
        if not isinstance(value, str) or len(value) > limits[field]:
            raise ValueError('注册信息格式或长度无效')
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ValueError('注册信息包含无效字符') from None
        data[field] = value if field == 'password' else value.strip()
    if not data['campusId']:
        raise ValueError('请输入数字校园号')
    if operation != 'campus':
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', data['email']):
            raise ValueError('请输入有效邮箱地址')
    if operation == 'register':
        if not data['name']: raise ValueError('请输入用户名')
        if not data['realName']: raise ValueError('请输入真实姓名')
        if data['grade'] not in GRADES: raise ValueError('请选择年级')
        if data['classGroup'] not in CLASSES: raise ValueError('请选择班级')
        if len(data['password']) < 8: raise ValueError('密码至少 8 位')
        if not re.fullmatch(r'[0-9]{6}', data['code']): raise ValueError('请输入 6 位数字验证码')
    return data


def forward(base_url, operation, data):
    """Return only public status/error, never cookies, OTPs or user objects."""
    if operation not in FIELDS:
        raise ValueError('不支持的注册操作')
    try:
        target = urlsplit(base_url)
        port = target.port or 443
    except ValueError:
        return 503, {'error': '统一账号服务地址配置无效'}, {}
    if (target.scheme != 'https' or not target.hostname or target.username or target.password
            or target.path not in ('', '/') or target.query or target.fragment):
        return 503, {'error': '统一账号服务地址配置无效'}, {}
    connection = http.client.HTTPSConnection(target.hostname, port, timeout=20)
    try:
        connection.request('POST', '/api/auth/' + operation,
                           json.dumps(data, ensure_ascii=False).encode('utf-8'),
                           {'Content-Type': 'application/json', 'Accept': 'application/json',
                            'Origin': f'https://{target.netloc}', 'User-Agent': 'Hatchery-Registration/1.0'})
        response = connection.getresponse()
        body = response.read(65537)
        if len(body) > 65536:
            raise ValueError('Oversized response')
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError('Invalid response')
        if 200 <= response.status < 300:
            if value.get('ok') is False or value.get('success') is False or value.get('error'):
                raise ValueError('Contradictory success response')
            return response.status, {'ok': True}, {}
        if 400 <= response.status < 500:
            message = value.get('error')
            message = message[:300] if isinstance(message, str) and message else '注册请求未通过，请检查填写信息'
            for field in ('password', 'code'):
                if data.get(field): message = message.replace(data[field], '***')
            headers = {}
            retry = response.getheader('Retry-After', '')
            if response.status == 429 and retry.isdigit():
                headers['Retry-After'] = str(min(3600, max(1, int(retry))))
            result = {'error': message}
            if headers.get('Retry-After'): result['retryAfter'] = int(headers['Retry-After'])
            return response.status, result, headers
        # Redirects are never followed; credentials cannot leave the configured origin.
        raise ValueError('Unexpected response status')
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException, ValueError, UnicodeError):
        message = ('注册结果暂未确认，请先尝试登录；不要连续重复提交。' if operation == 'register'
                   else '统一账号服务暂时无法确认结果，请稍后再试。')
        return 502, {'error': message, 'uncertain': True}, {}
    finally:
        connection.close()
