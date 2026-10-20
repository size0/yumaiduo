"""鉴权检查（替换 V5 之前补的那道闸）。

★★ 为什么必须断：
   ① 这是"后台不裸奔"的唯一保障 —— 一旦对外绑定又没有鉴权，
      任何人都能改配置、看订单。
   ② 而它最容易出的错是【写丢了】：我第一版把"拒绝启动"那段
      写丢了（脚本崩了没写进文件），而我又没核对 ⇒
      "对外+没密码"竟然照常启动。★ 静态断言就是为了防这个。
   ③ 默认必须不影响本机开发（绑 127.0.0.1 时不开鉴权）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

results = []


def check(label, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


SRC = (ROOT / "server.py").read_text(encoding="utf-8")

print("=" * 76)
print("一、★★★ 三个入口都要有闸门（漏一个就是一条裸奔）")
print("=" * 76)
check("★★ do_GET 有闸门",
      re.search(r"def do_GET.*?self\._auth_ok\(\)", SRC, re.S) is not None)
check("★★ do_PUT 有闸门",
      re.search(r"def do_PUT.*?self\._auth_ok\(\)", SRC, re.S) is not None)
check("★★ do_POST 有闸门",
      re.search(r"def do_POST.*?self\._auth_ok\(\)", SRC, re.S) is not None)
check("★★ 闸门在【入口】而不是散在各路由（只有 3 处调用）",
      SRC.count("self._auth_ok()") == 3, str(SRC.count("self._auth_ok()")))

print()
print("=" * 76)
print("二、★★ 插件端点必须豁免（它有自己的验签，且不带 Basic 头）")
print("=" * 76)
check("★★ 豁免路径是 /api/v1/",
      '_AUTH_EXEMPT = ("/api/v1/",)' in SRC)
check("★★ 豁免判定在鉴权之前（否则插件会被 401 掉，整条链路死）",
      SRC.find("startswith(_AUTH_EXEMPT)")
      < SRC.find("_check_auth(self.headers)"))

print()
print("=" * 76)
print("三、★★ 拒绝启动那一段必须真的在文件里（我写丢过一次）")
print("=" * 76)
check("★★ 有「拒绝启动」的判断", "拒绝启动" in SRC)
check("★★ 条件是「要鉴权且没密码」",
      re.search(r"if AUTH_REQUIRED and not AUTH_PASSWORD:", SRC) is not None)
check("★★ 它抛 SystemExit（不是静静地继续跑）",
      re.search(r"if AUTH_REQUIRED and not AUTH_PASSWORD:.*?raise SystemExit",
                SRC, re.S) is not None)
check("★★ 它在 ThreadingHTTPServer【之前】（晚了就等于没拦）",
      SRC.find("raise SystemExit") < SRC.find("server = ThreadingHTTPServer"))

print()
print("=" * 76)
print("四、★ 默认不影响本机开发")
print("=" * 76)
check("★ 默认 HOST 是本机",
      'TICKET_AGENT_HOST", "127.0.0.1"' in SRC)
check("★ 本机 HOST ⇒ 不鉴权（mode 没设时）",
      '_LOCAL_HOSTS' in SRC and 'return False, user, pwd' in SRC)
check("★ HOST 不是本机 ⇒ 自动打开鉴权",
      "HOST={HOST} 不是本机 ⇒ 自动打开鉴权" in SRC)
check("★ 端口也走环境变量（部署时不用改代码）",
      'TICKET_AGENT_PORT' in SRC)

print()
print("=" * 76)
print("五、★ 密码比较要恒定时间（别用 == 泄露前缀）")
print("=" * 76)
check("★★ 用 hmac.compare_digest", "compare_digest" in SRC)
check("★★ 用户名也恒定时间比较（别只比密码）",
      SRC.count("compare_digest") >= 2, str(SRC.count("compare_digest")))
check("★ 401 带 WWW-Authenticate（浏览器会弹登录框）",
      "WWW-Authenticate" in SRC)
check("★★ 401 不透露哪里错（只说 auth_required）",
      'auth_required' in SRC and "密码错" not in SRC)

print()
print("=" * 76)
print("六、★ 监听地址与启动日志")
print("=" * 76)
check("★ 绑 0.0.0.0 时日志会说明鉴权状态",
      "鉴权：" in SRC and "AUTH_WHY" in SRC)
check("★ 密钥不落盘（只从环境变量读）",
      "TICKET_AGENT_PASSWORD" in SRC
      and "save_credentials" not in SRC.split("_auth_config")[1][:600])

print()
print("=" * 76)
passed, total = sum(results), len(results)
print(f"结果: {passed}/{total} 项通过")
print("=" * 76)
sys.exit(0 if passed == total else 1)
