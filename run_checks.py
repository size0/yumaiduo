"""
run_checks.py —— 全站体检（唯一入口）

════════════════════════════════════════════════════════════════
★ 这个文件的由来，是个教训

  之前每次只验证"我改的那个东西对不对"，
  没有任何环节检查"我没改的东西是不是还好的"。
  结果：改配置系统时把 AI 助理整页弄坏了，而且是使用者先发现的。

  修法不是"下次小心点" —— 那是空话。
  修法是让「检查全站」变成一条命令，改任何东西前后都跑：

      python run_checks.py

  两次都过，才算没弄坏。

★ 目录约定

  tests/    自检（断言型 + 演示型）
  demos/    教学演示 —— 手动跑，不进这份体检
  legacy/   ★ 已删除。它曾经放着几个从 V5 搬来的模块，
            现在按「V5 只作参考、不得引用」的规则清掉了。
            需要那段逻辑就照文档自己写，不要搬。
════════════════════════════════════════════════════════════════
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# ★ Windows 上 stdout 默认 GBK，打不出 ↔ ★ ✓ − 这类字符会抛
#   UnicodeEncodeError。★ 必须放在【任何 print 之前】——
#   这些测试是模块级脚本，从第一行就开始跑。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass



HERE = Path(__file__).resolve().parent

TESTS = HERE / "tests"
WORKSPACE = HERE.parent

# (显示名, 脚本路径)  —— 顺序：先全站冒烟，再分项细节
SUITES = [
    ("当前行为回归（pytest）", TESTS / "_check_current_pytest.py"),
    ("全站冒烟（页面 + 接口 + 配置）",       TESTS / "_smoke.py"),
    ("全量对齐核对（我们的每一项 ↔ 良票文档）", TESTS / "_check_alignment.py"),
    ("★ 接线层（消息 → 编排 → 假良票）",     TESTS / "_check_service.py"),
    ("★ 对话 AI（模型答话 / 转人工 / 边界）",  TESTS / "_check_ai.py"),
    ("★ 标准 agent（工具选择 + 校验闸门）",    TESTS / "_check_agent.py"),
    ("★ 事实字段命名（各层是否同名）",       TESTS / "_check_fact_names.py"),
    ("★ 报价记录（模型/存储/作废机制）",     TESTS / "_check_quote_store.py"),
    ("★ 订单管理（建单/幂等/支付/交付）",    TESTS / "_check_order_store.py"),
    ("★ 万达出票交付（人工/幂等/自动发货）", TESTS / "_check_wanda_delivery.py"),
    ("★ 订单管理页（界面/API/出票接口）",     TESTS / "_check_orders_ui.py"),
    ("★ 插件签名 + 命令队列",                 TESTS / "_check_plugin_queue.py"),
    ("★ 前后端接口对齐审计",                  TESTS / "_audit_frontend_backend.py"),
    ("★ 端到端链路（识别→报价→下单→回调）",  TESTS / "_check_ticket_flow.py"),
    ("退票链路（申请/完成/未成）",          TESTS / "_check_refund.py"),
    ("良票接入层（签名/错误码/重试）",       TESTS / "_check_liangpiao.py"),
    ("良票接口层 + 回调（26 接口/7 事件）",   TESTS / "_check_liangpiao_api.py"),
    ("★ 万达插件桥（号池/座位图/映射）",     TESTS / "_check_wanda_bridge.py"),
    ("★ 万达成本解析（渠道/会员价）",         TESTS / "_check_wanda_cost.py"),
    ("★ 万达场次解析（showDate 无效/自己滤日期）", TESTS / "_check_wanda_showtime.py"),
    ("★ 渠道分流（万达/良票 + 没选座位也能报）", TESTS / "_check_channel_route.py"),
    ("★★ 万达手工出票（落单→填票码→发给买家）", TESTS / "_check_wanda_manual_ticket.py"),
    ("报价规则引擎（匹配/加价/限价）",       TESTS / "_check_quote_rules.py"),
    ("报价规则引擎（分档报价表（V5 对齐））",       TESTS / "_check_quote_tiers.py"),
    ("配置规格一致性",                     TESTS / "_check_settings.py"),
    ("状态机 / 文案契约",                  TESTS / "_check_contract.py"),
    ("报价 30 分钟有效期",                 TESTS / "_demo_expiry.py"),
    ("智能问答表",                        TESTS / "_check_qa.py"),
    ("完整链路模拟",                       TESTS / "_demo_flow.py"),
    ("死代码（未用导入 / 孤立常量 / 语法）",   TESTS / "_check_deadcode.py"),
    ("★ 参数审计（多余的 / 没人用的 / 我编的）", TESTS / "_check_params.py"),
    ("★★ 文档响应字段全量核对（195 个）",    TESTS / "_check_doc_fields.py"),
    ("★ 渠道文案覆盖（万达/良票）",          TESTS / "_check_channel.py"),
    ("票达人气泡清单",                     WORKSPACE / "_check_bubbles.py"),
    ("★ 提醒（观影 / 催收货 / 催下单 / 催好评）",  TESTS / "_check_reminders.py"),
    ("★★ 知识库（关键词→agent 能查到）",  TESTS / "_check_knowledge.py"),
    ("★ 退款时序（星期 / 时段 / 延迟）",  TESTS / "_check_refund_schedule.py"),
    ("★ 人工介入超时关闭",  TESTS / "_check_manual_control.py"),
    ("★ 时间窗 + 夜间识别提醒",  TESTS / "_check_time_window.py"),
    ("★ 首次打招呼 + picker 控件",  TESTS / "_check_greeting.py"),
    ("★ 兜底报价（开关 / 模式 / 限价）",  TESTS / "_check_fallback_quote.py"),
    ("★ 二次报价（开关 / 等待 / 优惠）",  TESTS / "_check_second_quote.py"),
    ("★ 订单拦截（手动 / 自动）",  TESTS / "_check_intercept.py"),
    ("★ 人类页面鉴权（Basic Auth）",  TESTS / "_check_auth.py"),
    ("★ 优先报价模式（规则集选择）",  TESTS / "_check_quote_priority.py"),
    ("★ 一拍就进单（买家直接拍下）",  TESTS / "_check_buy_now.py"),
    ("★ 订单卡片幂等（重投不重复落单）",  TESTS / "_check_card_idempotent.py"),
    ("★ 店铺自动发送开关（关掉就不发）",  TESTS / "_check_shop_send_gate.py"),
    ("★ 店铺开关接口（PUT/落盘/非法值）",  TESTS / "_check_shops_api.py"),
    ("★ 出票方式/换座状态（真传下去）",  TESTS / "_check_release_mode.py"),
    ("★ 自动改价开关（开/关都验）",  TESTS / "_check_auto_reprice_toggle.py"),
    ("★ 保存配置（取票图空值不再拦保存）",  TESTS / "_check_settings_save.py"),
    ("★ 配置字段类型（字符串不许存成 bool）",  TESTS / "_check_settings_types.py"),
    ("★ 票速设置（复选框组不许存丢）",  TESTS / "_check_speed_tiers_ui.py"),
    ("★ 操作台文案（不许光秃秃的英文）",  TESTS / "_check_ui_chinese.py"),
    ("★ 黑名单（鱼麦多接口，查不到不许拦人）",  TESTS / "_check_blacklist.py"),
]

# 只演练、不断言的：跑了只为确认不报错，不计入成败
DEMOS = [
    ("架构审计（分层/循环/孤儿）",  TESTS / "_audit_architecture.py"),
    # ★ 纪律检查：V5 只作参考、不得引用
    ("V5 引用审计",                TESTS / "_audit_v5_taint.py"),
]


def tail(text: str) -> str:
    """
    从输出里挑一行摘要。

    ★ 判据要够严：优先找【同时含数字和"通过"】的行。
      各套测试的小节标题叫「结果」，含"结果"但没数字 ——
      早先按"结果开头"去找，摘要就变成光秃秃的"结果"两个字。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if "通过" in ln and any(ch.isdigit() for ch in ln):
            return ln[:110]
    for ln in reversed(lines):
        if "通过" in ln:
            return ln[:110]
    return lines[-1][:110] if lines else "（无输出）"


def run(path: Path, timeout: int = 600):
    """跑一个脚本，返回 (退出码, 输出)。"""
    try:
        # ★ 给子进程显式设 UTF-8 环境 —— 两层保险。
        #   子脚本自己开头会 reconfigure（见各脚本开头那段），
        #   这里再设一遍环境变量兜住。
        #   ★ 为什么值得两层：这个编码坑在本项目翻车过三次，
        #     而且第三次的代价是【22 个文件被误删了模块级声明】——
        #     因为修它的脚本自己写错了。宁可多一层。
        _env = {**os.environ, "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1"}
        proc = subprocess.run(
            [sys.executable, str(path)], cwd=str(path.parent), env=_env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "超时"
    except Exception as exc:                        # noqa: BLE001
        return 1, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run application checks.")
    parser.add_argument("--current", action="store_true",
                        help="Run only the current behavior suite on an isolated copy.")
    args = parser.parse_args()
    suites = ([row for row in SUITES if row[1] == TESTS / "_check_current_pytest.py"]
              if args.current else SUITES)
    print()
    print("=" * 74)
    print(" 当前行为检查（独立副本）" if args.current else " 全站体检")
    print("=" * 74)

    failures, skipped = [], []
    started = time.time()

    for name, path in suites:
        if not path.exists():
            print(f"  ??  {name:<32} 文件不存在: {path.name}")
            skipped.append(name)
            continue
        code, out = run(path)
        mark = "OK" if code == 0 else "XX"
        print(f"  {mark}  {name:<32} {tail(out)}")
        if code != 0:
            failures.append((name, path))
            if args.current:
                print(out)
            for line in out.splitlines():
                if line.startswith("[FAIL]") or "★" in line:
                    print(f"        {line.strip()}")

    elapsed = time.time() - started
    print("-" * 74)

    # 审计类只报告，不影响成败
    for name, path in ([] if args.current else DEMOS):
        if not path.exists():
            continue
        code, out = run(path, timeout=120)
        # 审计脚本会返回非 0 表示"发现问题"，这里要看出来
        mark = "--" if code == 0 else "!!"
        summary = ""
        for line in out.splitlines():
            if "汇总" in line:
                continue
            if any(k in line for k in ("分层违规", "循环依赖", "孤儿模块", "超大文件",
                                       "结果：")):
                summary += line.strip() + "  "
        print(f"  {mark}  {name:<32} {summary.strip() or ('退出码 ' + str(code))}")

    print("=" * 74)
    if args.current and not failures and not skipped:
        print(f" 当前行为检查通过（{elapsed:.1f} 秒）；不代表旧门禁已通过。")
        return 0
    if not failures and not skipped:
        print(f" 全部通过（{len(SUITES)} 套，{elapsed:.1f} 秒）—— 可以继续。")
        n_demo = len(list((HERE / "demos").glob("*.py")))
        print(f" demos/ 里的 {n_demo} 个教学演示不进体检（要真模型，手动跑）")
        print("=" * 74)
        return 0

    if failures:
        print(f" 有 {len(failures)} 套没过：")
        for name, path in failures:
            print(f"   ✗ {name}   →  python {path.relative_to(WORKSPACE)}")
    if skipped:
        print(f" 跳过 {len(skipped)} 套（文件不存在）")
    print(" 先修完再往下做 —— 别在坏的基础上继续加。")
    print("=" * 74)
    return 1


if __name__ == "__main__":
    sys.exit(main())
