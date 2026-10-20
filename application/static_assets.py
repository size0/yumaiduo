"""Content-versioned local CSS/JS; HTML and business data remain uncached."""
import hashlib
import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit


def digest(body):
    return hashlib.sha256(body).hexdigest()


def version_links(html, web):
    web = web.resolve()

    def replace(match):
        url = urlsplit(match['url'])
        if url.scheme or url.netloc or url.path.startswith('/'):
            return match[0]
        target = (web / url.path).resolve()
        if web not in target.parents or not target.is_file():
            return match[0]
        params = [(k, v) for k, v in parse_qsl(url.query) if k != 'v']
        params.append(('v', digest(target.read_bytes())[:16]))
        link = urlunsplit(('', '', url.path, urlencode(params), url.fragment))
        return match['attr'] + link + match['end']

    return re.sub(r'''(?P<attr>\b(?:src|href)\s*=\s*["'])(?P<url>[^"']+\.(?:css|js)(?:\?[^"']*)?)(?P<end>["'])''', replace, html)


def cache_headers(body, query):
    hash_value = digest(body)
    version = parse_qs(query).get('v', [''])[0]
    policy = 'private, max-age=31536000, immutable' if version == hash_value[:16] else 'private, no-cache'
    return policy, '"' + hash_value + '"'
