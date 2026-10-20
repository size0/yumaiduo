"""Chat Completions client for DeepSeek and operator-configured GPT proxies.

Supports injected transport, tool calls, bounded outputs and typed failures.
The conversation layer chooses tools; this client never executes business actions.
Provider-specific request parameters stay separate. Tests can use fake transport
without a key, network request or model charge.
"""

import json
import http.client
import socket
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from contextlib import closing
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
# 答一句话，不需要长输出。给足思考空间但别浪费。
DEFAULT_MAX_TOKENS = 1024
# ★ 对话答复超时 20 秒 —— 这是我选的值，文档没给。
#   依据：买家在等回复，等太久不如直接转人工。
#   20 秒比良票那些接口（10~12 秒）宽松，因为模型首字延迟比普通 API 高。
#   ⇒ 如果上线后发现经常超时，先调这个值，不用去查文档（文档里没有）。
DEFAULT_TIMEOUT = 20.0
# 重试 1 次：模型偶发抖动值得再试一次，但别让买家等第二轮
DEFAULT_RETRIES = 1

# ★ 只有这些模型可配（与 settings 的 model_options 对齐）
KNOWN_MODELS = ("deepseek-flash", "deepseek-v4-pro")


# ==================================================================== 错误
class ModelError(Exception):
    """调模型失败。★ 上层应当【转人工】，不要给买家看这个。"""

    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


# ==================================================================== 响应
@dataclass
class ModelReply:
    """一次模型调用的结果。"""
    content: str = ""
    reasoning: str = ""                 # 思考过程，仅日志用，不回传给买家
    raw: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return int(self.usage.get("total_tokens") or 0)


# ==================================================================== 假传输
class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Resolve once, connect a validated public IP, retain TLS hostname checks."""
    def connect(self):
        from .connection import public_addresses
        addresses = public_addresses(self.host, self.port)
        last_error = None
        deadline = time.monotonic() + float(self.timeout)
        for family, kind, proto, _, address in addresses[:8]:
            sock = socket.socket(family, kind, proto)
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('模型连接超时')
                sock.settimeout(remaining)
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except (OSError, ValueError) as exc:
                sock.close()
                last_error = exc
        raise last_error or OSError('模型接口未能建立连接')


class UrllibTransport:
    """真实 HTTP。★ 单独一层，方便测试时换掉。"""

    def __init__(self, *, custom_endpoint=False):
        self.custom_endpoint = custom_endpoint

    def post(self, url: str, body: dict, headers: dict, timeout: float) -> tuple:
        if self.custom_endpoint:
            from .connection import base_url
            try:
                base_url(url)
                parsed = urlsplit(url)
                # Direct HTTPS has no proxy or automatic redirect behavior.
                with closing(PinnedHTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)) as conn:
                    conn.request('POST', parsed.path, body=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers=headers)
                    response = conn.getresponse()
                    return response.status, response.read().decode('utf-8', 'replace')
            except (ValueError, OSError, http.client.HTTPException):
                raise ModelError('中转模型连接失败，请核对接口地址及网络') from None
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise ModelError(f"连不上模型服务：{exc.reason}", retryable=True) from exc
        except TimeoutError as exc:
            raise ModelError("模型响应超时", retryable=True) from exc


# ==================================================================== 客户端
class ModelClient:
    """
    对话模型客户端。

    ★ 依赖注入：transport / sleep 都可换。
      没有 API Key 时构造不报错，调用时才报 —— 这样服务能起来，
      界面能打开，只是答不了问题（然后转人工）。
    """

    def __init__(self, api_key: str = "", *, model: str = DEFAULT_MODEL,
                 base_url: str = DEFAULT_BASE_URL,
                 protocol: str = 'deepseek',
                 transport: Any = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 retries: int = DEFAULT_RETRIES,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 thinking: bool = False,
                 temperature: float = 0.7,
                 sleep=time.sleep,
                 log=None):
        self.api_key = api_key or ""
        if protocol not in ('deepseek', 'openai_chat'):
            raise ValueError('不支持的模型接口协议')
        self.protocol = protocol
        self.model = model if protocol == 'openai_chat' or model in KNOWN_MODELS else DEFAULT_MODEL
        if protocol == 'openai_chat':
            from .connection import base_url as validate_base
            self.base_url = validate_base(base_url)
            if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(c.isspace() for c in model):
                raise ValueError('请填写中转站提供的模型ID')
        else:
            self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.transport = transport or UrllibTransport(custom_endpoint=protocol == 'openai_chat')
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.max_tokens = max_tokens
        # ★ 默认关思考：对话答复要快、要便宜
        self.thinking = thinking
        self.temperature = temperature
        self.sleep = sleep
        self.log = log
        # 记账（便于观察花了多少）
        self.calls = 0
        self.total_tokens = 0
        self.last_error = ""

    # ---------------------------------------------------------------- 可用性
    @property
    def ready(self) -> bool:
        """能不能真的调用。没 Key 就是没准备好。"""
        return bool(self.api_key)

    # ---------------------------------------------------------------- 调用
    def chat(self, messages: list, *, system: str = "",
             max_tokens: int | None = None,
             temperature: float | None = None,
             tools: list | None = None) -> ModelReply:
        """
        问一句，拿回一句话（或者一次工具调用请求）。

        messages 形如 [{"role": "user", "content": "..."}]，不含 system。
        system 单独传 —— 因为 system prompt 每次都要重新拼（模型无状态）。

        tools：原生 function calling 的 schema 列表。
               ★ 传了就可能返回 tool_calls（在 ModelReply.raw 里）——
                 调用方负责执行工具并把结果作为 role="tool" 追加回去。
                 本客户端【不】替你跑循环，因为"最多几轮"是业务决定。
        """
        if not self.ready:
            raise ModelError("没有配置模型 API Key")

        full = []
        if system:
            full.append({"role": "system", "content": system})
        full.extend(messages)
        if not full:
            raise ModelError("messages 不能为空")

        payload: dict = {
            "model": self.model,
            "messages": full,
        }
        if self.protocol == 'deepseek':
            payload['max_tokens'] = max_tokens or self.max_tokens
            payload['thinking'] = {'type': 'enabled' if self.thinking else 'disabled'}
        else:
            payload['max_completion_tokens'] = max_tokens or self.max_tokens
        # ★ 思考模式下 temperature 无效，只在非思考时传
        if self.protocol == 'deepseek' and not self.thinking:
            payload["temperature"] = (self.temperature if temperature is None
                                      else temperature)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        url = f"{self.base_url}/chat/completions"

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                status, text = self.transport.post(url, payload, headers, self.timeout)
            except ModelError as exc:
                last_exc = exc
                if not exc.retryable or attempt >= self.retries:
                    raise
                self.sleep(0.5 * (2 ** attempt))
                continue

            if status == 200:
                try:
                    reply = self._parse(text)
                except ModelError:
                    if self.protocol == 'openai_chat':
                        raise ModelError('中转模型未返回有效的Chat Completions结果') from None
                    raise
                self.calls += 1
                self.total_tokens += reply.tokens
                return reply

            # ---- 错误分类 ----
            detail = (self._error_detail(text) if self.protocol == 'deepseek'
                      else '中转模型请求失败，请核对接口、模型权限及余额')
            if self.api_key:
                detail = detail.replace(self.api_key, '[REDACTED]')
            self.last_error = f"HTTP {status} {detail}"
            if self.log:
                self.log(f"[model] {self.last_error}")

            if status in (401, 403):
                # Key 不对，重试没意义
                raise ModelError(f"模型鉴权失败（HTTP {status}）：{detail}",
                                 status=status)
            if status == 429:
                # 限流：等一下再试
                if attempt >= self.retries:
                    raise ModelError(f"模型限流（HTTP 429）：{detail}",
                                     status=status, retryable=True)
                self.sleep(1.0 * (2 ** attempt))
                continue
            if status >= 500:
                if attempt >= self.retries:
                    raise ModelError(f"模型服务异常（HTTP {status}）：{detail}",
                                     status=status, retryable=True)
                self.sleep(1.0 * (2 ** attempt))
                continue
            # 400 之类：请求本身有问题，别重试
            raise ModelError(f"模型拒绝了请求（HTTP {status}）：{detail}",
                             status=status)

        raise last_exc or ModelError("模型调用失败")

    # ---------------------------------------------------------------- 解析
    def _parse(self, text: str) -> ModelReply:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError(f"模型返回的不是 JSON：{text[:120]}") from exc

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise ModelError(f"模型报错：{msg}")

        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"模型没有返回 choices：{text[:120]}")
        msg = choices[0].get("message") or {}

        content = (msg.get("content") or "").strip()
        # ★ reasoning_content 只留日志，不回传给买家
        reasoning = (msg.get("reasoning_content") or "").strip()
        return ModelReply(content=content, reasoning=reasoning,
                          raw=msg, usage=data.get("usage") or {})

    @staticmethod
    def _error_detail(text: str) -> str:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    return str(err.get("message") or err)[:160]
                return str(data.get("message") or data)[:160]
        except json.JSONDecodeError:
            pass
        return (text or "")[:160]

    # ---------------------------------------------------------------- 记账
    def usage_text(self) -> str:
        """给界面/日志看的一句账。"""
        return f"调用 {self.calls} 次，共 {self.total_tokens} tokens"
