"""
integrations/yumaiduo —— 鱼麦多插件对接层

★ 这个包干什么

  鱼麦多插件跑在【卖家的闲鱼店铺】里，它负责：
    · 收买家消息、发消息
    · 改价、发货、取消订单
    · 读商品、读订单

  但它【不会自己决定做什么】—— 它是"手"，不是"脑"。
  脑在我们这边：我们产生命令，它来领命令去执行，再把结果回执给我们。

  ⇒ 所以这个包要做两件事：
      ① 校验插件签名（它每次请求都带 HMAC 签名）
      ② 把"买家消息"翻译成"要执行的动作"（命令）

★ 一次买家消息的完整往返

    plugin ──POST /api/v1/events──→ 我们（存事件 + 生成命令）
    plugin ──POST /api/v1/commands/claim──→ 我们（领走待执行命令）
    plugin 执行（比如"发这句话给买家"）
    plugin ──POST /api/v1/commands/{id}/result──→ 我们（记回执）
"""

from .signature import DEFAULT_WINDOW_SECONDS, sign, verify

__all__ = ["DEFAULT_WINDOW_SECONDS", "sign", "verify"]
