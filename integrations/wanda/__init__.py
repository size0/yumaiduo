"""
integrations/wanda —— 万达接入层。

════════════════════════════════════════════════════════════════
★★★ 架构决定（2026-10-13）：走【出票系统的插件桥】，不直连万达官方

  我们【不】自己调万达官方接口。原因不是省事，是**账号租约**：

      出票系统的账号池规定「一个手机号同时只允许一个订单占用」
      （acquire_account_lease / account_busy）。

      ★ 但这个租约【只在出票系统进程内有效】。
        如果我们也拿 token 直连万达，出票系统就不知道我们在用这个号
        ⇒ 同一个万达账号被两个进程同时使用
        ⇒ 锁座冲突、活动资格被抢、触发风控。

  另外直连还要我们自己维护：token / client_key / device_id / shumei_box_id、
  MD5 签名（cCode=1_2/1_3、ver 随版本变、连字段名拼写都有坑）、
  各家域名的错误口径、缓存…… 而这些出票系统全都做好了。

  ⇒ 分工：我们只做「成本 + 定价」，出票系统做「凭证 + 账号 + 传输」。

════════════════════════════════════════════════════════════════
★ 本目录现在只有两个模块

  bridge.py   出票系统插件桥的只读客户端（★ 唯一在用的）
              · 号池      /api/auth/internal/wplus-accounts
              · 座位图    /api/showtime/seats
              · 影院映射  /api/cinema/wanda-lookup
              只发 GET，不含任何万达签名代码。
              ★ 已实测：/api/showtime/seats?showtimeId=103172465 能拿到
                座位图，且 area[].wPlusActivity.price 就是会员价（明文、按区域）。

  fields.py   万达响应字段的来源对照表（纯数据，无依赖）
              排查"为什么读不到会员价"时用得上：
              它写清每个字段来自响应的哪个路径
              （如 area[].wPlusActivity.price → 会员价）。

════════════════════════════════════════════════════════════════
★ 已删除：直连万达官方那一套（1756 行）

  删掉的文件：api.py(621) client.py(429) signature.py(333)
              matcher.py(304) 以及 __init__ 里给它们的导出
  连带删掉的测试：tests/_check_wanda.py（189 项）
                  tests/_live_probe.py（要凭证才能跑的探针）

  ★ 为什么删而不是留着：
    · 生产代码【零 import】—— 是"写好了但没接上"的典型
    · 它【误导过我】：我前几轮一直在研究"怎么自己调万达
      real_time_seat / create_order"，直到拿到架构说明才知道该走桥
    · 留着会让后来人（包括下一个 AI）重走一遍弯路

  ★ 删除前搬走的东西（不能丢的）：
    · 「会员价不能高于原价」这条判据
      （V5 providers/wanda/cost.py:62-64 'activity cost exceeds original price'）
      → 已搬进 application/wanda_cost.py 的 MEMBER_PRICE_ABOVE_ORIGINAL
    · fields.py 的字段对照表 → 保留
"""
