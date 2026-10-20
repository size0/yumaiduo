"""Validate operator-configured model endpoints without making a model call."""
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


def is_public(value):
    address = ipaddress.ip_address(value)
    return address.is_global and not address.is_multicast and not address.is_reserved


def public_addresses(host, port):
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError('无法解析模型接口域名') from None
    if not addresses or any(not is_public(row[4][0]) for row in addresses):
        raise ValueError('模型接口解析到本机或内网，已拒绝连接')
    return addresses


def base_url(value, *, resolve=False):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError('请填写有效的HTTPS接口地址')
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        port = parsed.port
    except ValueError:
        raise ValueError('接口地址格式不正确') from None
    if (parsed.scheme != 'https' or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('接口地址须为HTTPS，不能包含账号、密钥、查询参数或片段')
    if host.lower().rstrip('.').endswith(('.localhost', '.local')) or host.lower().rstrip('.') == 'localhost':
        raise ValueError('模型接口不能指向本机或内网')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if port == 0:
        raise ValueError('接口端口须为1至65535')
    if address is not None and not is_public(host):
        raise ValueError('模型接口不能指向本机或内网')
    if resolve:
        public_addresses(host, port or 443)
    path = parsed.path.rstrip('/')
    if path.endswith('/chat/completions'):
        path = path[:-len('/chat/completions')]
    authority = '[' + host + ']' if ':' in host else host.encode('idna').decode('ascii')
    if port is not None:
        authority += ':' + str(port)
    return urlunsplit(('https', authority, path, '', ''))
