"""One reply policy for completion, evidence and promises; no intent routing."""
from dataclasses import dataclass
from decimal import Decimal
import json
import re

RESPONSE_SCHEMA = ('{"answer":"给买家的文字","intent":"other/inquire/revise/reprice",'
                   '"kind":"explanation/acknowledgement/clarification/result","claims":[]}')


@dataclass(frozen=True)
class ReplyAssessment:
    text: str
    feedback: str = ""
    fallback: str = ""
    reason: str = "ACCEPTED"


def unpack(text: str) -> tuple[str, dict]:
    """Response metadata is descriptive; it never grants tool permission."""
    try:
        candidate = str(text or "").strip()
        if candidate.startswith("```") and candidate.endswith("```"):
            candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(candidate)
    except (ValueError, TypeError):
        return str(text or ""), {}
    if (not isinstance(data, dict) or not isinstance(data.get("answer"), str)
            or data.get("intent") not in ("other", "inquire", "revise", "reprice")
            or data.get("kind") not in ("explanation", "acknowledgement", "clarification", "result")
            or not isinstance(data.get("claims"), list)):
        return str(data.get("answer") or "") if isinstance(data, dict) else "", {"invalid_response_metadata": True}
    return data["answer"], {key: data[key] for key in ("intent", "kind", "claims")}


def _affirmed(pattern: str, text: str) -> bool:
    for match in re.finditer(pattern, text):
        before = text[max(0, match.start() - 8):match.start()]
        after = text[match.end():match.end() + 8]
        # A conditional process explanation is not a completed-operation claim.
        if re.match(r"(?:之后|以后|后|时|的话)", after):
            continue
        clause = re.split(r"[，。；！？\n]", text[:match.start()])[-1]
        if re.match(r"\s*(?:如果|若|假如|待|等到)", clause):
            continue
        if re.search(r"(?:不要|不能|勿|并非|不是|未|不支持|不代表|无需|不用)(?:说|保证|会|能|再|去|先)?$", before):
            continue
        if re.search(r"不要|不能|勿|无需|不用", match.group()):
            continue
        return True
    return False


def _unsupported_amount(tools, text: str) -> bool:
    from .quote_comparison import lookup,render,money_literals
    comparison=lookup(tools)
    amounts = [left or right for left, right in re.findall(
        r"[¥￥]\s*([0-9]+(?:\.[0-9]+)?)|([0-9]+(?:\.[0-9]+)?)\s*(?:元|块)", text)]
    if getattr(tools,'seller_context',{}).get('active'):
        from .seller_context import price_literals
        amounts.extend(value for value,_ in price_literals(text))
    if comparison:
        amounts.extend(money_literals(text))
    if not amounts:
        return False
    allowed = set()
    from .seller_context import amounts as seller_amounts
    allowed.update(seller_amounts(tools,text))
    # A successful tool can replace the quote after the initial snapshot.
    try:
        quote_result = tools.read_quote()
        quote = quote_result.get("quote")
    except Exception:
        quote = None
    if quote and getattr(tools.conv, "demand_id", ""):
        if (quote.get("demand_id") != tools.conv.demand_id
                or quote.get("demand_rev") != tools.conv.demand_rev):
            quote = None
    if quote and quote.get("valid"):
        allowed.update(str(quote[key]) for key in ("unit_yuan", "total_yuan") if quote.get(key))
    # A comparison's typed price paragraph must remain intact; bare numbers
    # would allow a model to swap unit/total or rule/screenshot meanings.
    if comparison:
        paragraph=render(comparison)
        if paragraph in text:
            remainder=text.replace(paragraph,'',1)
            return bool(money_literals(remainder))
        return True
    platform = getattr(tools, "platform_facts", {})
    from .purchase_continuation import separate
    if platform.get("ok") and (not separate(tools) or str(platform.get('order_no') or 'MISSING_ORDER') in text):
        allowed.update(str(platform[key]) for key in ("platform_amount_yuan", "latest_quote_yuan")
                       if platform.get(key) is not None)
    # Configured policy may describe a fee; that is not permission to charge it.
    for name, result in getattr(tools, "tool_results", []):
        if name == "search_faq" and result.get("ok"):
            policy = str(result.get("answer") or "") + str(result.get("shop_experience") or "")
            allowed.update(re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(?:元|块)", policy))
    return not all(Decimal(value) in {Decimal(number) for number in allowed} for value in amounts)


def _order_rows(tools) -> list[dict]:
    rows = list((getattr(tools, "context_snapshot", {}).get("orders") or {}).get("orders") or [])
    for name, result in getattr(tools, "tool_results", []):
        if name in ("read_order", "read_refund_status") and result.get("ok"):
            rows = result.get("orders") or []
    return rows


def _is_current_order(tools, row: dict) -> bool:
    demand_id = getattr(tools.conv, "demand_id", "")
    if demand_id:
        return bool(row.get("demand_id") == demand_id
                    and row.get("demand_rev") == getattr(tools.conv, "demand_rev", 0))
    return bool(tools.conv.quote_id and row.get("quote_id") == tools.conv.quote_id)


def _unsupported_success(tools, text: str) -> bool:
    rows = _order_rows(tools)
    explicit = {claim.get("order_no") for claim in getattr(tools, "reply_metadata", {}).get("claims", [])
                if isinstance(claim, dict) and claim.get("kind") in ("order", "platform_order")
                and claim.get("order_no") and claim["order_no"] in text}
    rows = [row for row in rows if row.get("order_no") in explicit] if explicit else [
        row for row in rows if _is_current_order(tools, row)]
    if len(rows) != 1:
        rows = []
    evidence = {
        "order": any(row.get("platform_order_exists") for row in rows),
        "payment": any(row.get("payment_state") == "CONFIRMED" for row in rows),
        "shipment": any(row.get("platform_shipped") for row in rows),
        "ticket": any(row.get("status") in ("TICKETED", "DELIVERED")
                      and row.get("ticket_image_available") for row in rows),
        "refund": any(row.get("status") == "REFUNDED" and row.get("payment_state") == "REFUNDED"
                      for row in rows),
    }
    patterns = {
        "order": r"已(?:经)?(?:帮你)?下单|下单(?:成功|完成)|订单已(?:经)?(?:提交|创建)",
        "payment": r"已(?:经)?(?:付款|支付)|(?:付款|支付)(?:成功|完成)",
        "shipment": r"已(?:经)?发货|发货(?:成功|完成)",
        "ticket": r"出票(?:成功|完成)|已(?:经)?出票",
        "refund": r"已(?:经)?退款|退款(?:成功|完成)|已(?:经)?退回",
    }
    return any(not evidence[kind] and _affirmed(pattern, text) for kind, pattern in patterns.items())


def _claim_feedback(tools, text: str) -> str:
    metadata = getattr(tools, "reply_metadata", {})
    if metadata.get("invalid_response_metadata"):
        return '回复结构不完整，请返回完整JSON对象，格式：' + RESPONSE_SCHEMA
    for claim in metadata.get("claims", []):
        if not isinstance(claim, dict):
            return '事实声明必须包含类型和权威记录引用。'
        kind = claim.get("kind")
        if kind == "quote":
            try:
                quote = tools.read_quote().get("quote") or {}
            except Exception:
                quote = {}
            if (not quote.get("valid") or not quote.get("demand_id")
                    or any(claim.get(key) != quote.get(key) for key in ("quote_id", "demand_id", "demand_rev"))
                    or quote.get("demand_id") != getattr(tools.conv, "demand_id", "")
                    or quote.get("demand_rev") != getattr(tools.conv, "demand_rev", 0)):
                return '报价声明与当前有效需求版本不一致，请查询当前报价后使用真实引用。'
        elif kind == "order":
            rows = [row for row in _order_rows(tools) if row.get("order_no") == claim.get("order_no")]
            if (len(rows) != 1 or (not _is_current_order(tools, rows[0])
                                  and str(claim.get("order_no") or "") not in text)):
                return '订单声明缺少唯一引用；说明历史订单时须写明订单号，不能把历史状态当作当前需求。'
        elif kind == "platform_order":
            facts = getattr(tools, "platform_facts", {})
            if not facts.get("ok") or claim.get("order_no") != facts.get("order_no"):
                return '平台状态声明缺少本轮实际查询的订单引用。'
        elif kind == "operation":
            operations = {result.get("operation_id") for _, result in getattr(tools, "tool_results", [])
                          if result.get("outcome") == "REQUESTED" and result.get("operation_id")}
            if claim.get("operation_id") not in operations or claim.get("state") != "REQUESTED":
                return '只能说明真实创建的请求，不能把待执行操作声明为已成功。'
        elif kind == "tool_failure":
            observation = failure_observation(tools, reference_tool=str(claim.get('tool') or ''))
            if (not observation or claim.get('tool') != observation[0]
                    or claim.get('status') != observation[1].get('status')):
                return '失败说明必须引用本轮尚未解决的真实工具名及状态，不能引用旧失败或虚构原因。'
        elif kind == 'seller_message':
            from .seller_context import referenced_messages,price_reference_valid
            rows=referenced_messages(tools)
            if (not any(r['message_id']==claim.get('message_id') for r in rows)
                    or not re.search('店家|人工|店铺',text)):
                return '人工价格只能注明店家来源，引用当前需求内真实店铺消息；不能当成自动报价或交易授权。'
            if not price_reference_valid(tools,text):
                return '只能转述店家原话明确的金额及单价/总价口径，不能将座号当价格或把单价说成总价。'
        else:
            return '该事实声明尚无可核验的引用，请先使用对应查询工具；不要填写虚构声明。'
    return ''


def assess(tools, text: str) -> ReplyAssessment:
    """One policy used for model feedback and the final send boundary."""
    text = str(text or "")
    failure = failure_reply(tools) if getattr(tools, "semantic_mode", False) else ""
    metadata = getattr(tools, 'reply_metadata', {})
    explains_failure = (metadata.get('kind') in ('explanation', 'clarification')
        and not _claim_feedback(tools, text))
    failed_completion_claim = bool(failure and _affirmed(
        r'(?:已(?:经)?|成功)[^，。；！？\n]{0,20}(?:保存|更新|修改|记录|记下|重新报价|改|调)|'
        r'(?:保存|更新|修改|重新报价)(?:成功|完成)', text))
    if failed_completion_claim:
        explains_failure = False
    if failure and not explains_failure:
        # Preserve an actual handoff decision when the observation requires a
        # payment/order investigation. Do not turn parameter errors into handoffs.
        blocked = _failure_needs_handoff(tools)
        if text.startswith('[转人工]') and blocked:
            failure = '[转人工]' + failure
        return ReplyAssessment(failure,
            '请依据工具的真实失败结果解释原因或只问缺项，不声称完成。'
            '最终JSON的kind用explanation或clarification，claims引用'
            '[{"kind":"tool_failure","tool":"实际工具名","status":"实际失败状态"}]；'
            '不得编造付款、报价或执行成功。' + RESPONSE_SCHEMA,
            failure, "REQUEST_FAILED")
    # Code-generated projections have already passed the same factual checks.
    if text and text == getattr(tools, "safe_reply_projection", None):
        return ReplyAssessment(text, reason="TOOL_PROJECTION")
    observations = [(name, result) for name, result in getattr(tools, "tool_results", [])
                    if name != "record_buyer_intent"]
    if observations and observations[-1][0] == "get_purchase_guidance":
        guidance = observations[-1][1]
        if guidance.get("payment_blocked") and guidance.get("guidance"):
            tools.safe_reply_projection = guidance["guidance"]
            return ReplyAssessment(guidance["guidance"], fallback=guidance["guidance"], reason="TOOL_PROJECTION")
    reprice_claim = _affirmed(
        r"(?:价格|金额)(?:已经|已)(?:更新|调整|修改|变更)|"
        r"(?:价格|金额)(?:已经|已)?(?:更新|调整|修改|变更)(?:为|成|好|成功)|"
        r"(?:已经|已)(?:给你|帮你|替你|为你)?(?:改|调)(?:价)?(?:好|完|过|成功)|"
        r"(?:已经|已)(?:给你|帮你|替你|为你)?(?:改价|调价)|"
        r"(?:改|调)(?:价|价格|金额)?(?:好了|完了|成功|过了)|"
        r"(?:改价|调价|价格调整|金额调整)(?:已经|已)?(?:成功|完成|好)", text)
    payment_invitation = _affirmed(
        r"(?:直接|现在|就|可以|请).{0,10}(?:付款|支付|付钱|付了)|(?:付款|支付|付钱)(?:即可|吧|就行|一下)",
        text)
    checkout_invitation = _affirmed(
        r"(?:直接|现在|可以|请).{0,8}(?:拍下|下单)|(?:拍下|下单)(?:吧|即可|就行)", text)
    success_claim = _unsupported_success(tools, text)
    wanda_auto = (tools.conv.channel == "wanda"
                  and _affirmed("自动出票", text))
    feedback = completion_feedback(tools, text) or response_feedback(tools, text)
    if feedback and not (reprice_claim or payment_invitation or wanda_auto or success_claim or checkout_invitation):
        return ReplyAssessment(unfinished_reply(tools), feedback, reason="NEEDS_REPAIR")
    amount_claim = _unsupported_amount(tools, text)
    if failure and (reprice_claim or payment_invitation or checkout_invitation or amount_claim or success_claim or wanda_auto):
        if text.startswith('[转人工]') and _failure_needs_handoff(tools):
            failure='[转人工]'+failure
        return ReplyAssessment(failure,'失败结果不能证明交易或需求已成功，请依据当前失败说明收尾。',failure,'UNVERIFIED_TRANSACTION_CLAIM')
    seller=getattr(tools,'seller_context',{})
    if seller.get('active') and (amount_claim or reprice_claim or payment_invitation or checkout_invitation):
        return ReplyAssessment(seller['guidance'],'请依据店家最新回复解释价格来源，旧自动价已暂停。',seller['guidance'],'SELLER_REVIEW_REQUIRED')
    if amount_claim and not (failure or reprice_claim or payment_invitation or checkout_invitation or success_claim or wanda_auto):
        from .quote_comparison import explanation
        paragraph=explanation(tools)
        if paragraph:
            return ReplyAssessment(paragraph,reason='COMPARISON_PRICE_PROJECTION')
    if not (reprice_claim or payment_invitation or wanda_auto or amount_claim or success_claim or checkout_invitation):
        return ReplyAssessment(text)
    try:
        facts = getattr(tools, "platform_facts", None)
        guidance = facts if facts else tools.get_purchase_guidance()
        guidance = dict(guidance, payment_blocked=guidance.get("payment_blocked", True))
    except Exception:
        guidance = {"ok": False, "guidance": "当前订单及改价结果还未核实，请先不要付款，等待店家核对。"}
    if (checkout_invitation and not (reprice_claim or payment_invitation or success_claim or amount_claim or wanda_auto)
            and guidance.get("template")):
        return ReplyAssessment(text)
    if reprice_claim or amount_claim or success_claim or not guidance.get("ok") or guidance.get("payment_blocked"):
        safe = guidance.get("guidance") or guidance.get("reason") or "改价结果尚未确认，请先不要付款，等待店家核对。"
    elif wanda_auto:
        safe = "万达订单由店家处理出票，取票信息以实际出票结果为准。" + (guidance.get("guidance") or "")
    else:
        return ReplyAssessment(text)
    tools.safe_reply_projection = safe
    return ReplyAssessment(safe, "交易声明必须依据当前工具核验结果。", safe, "UNVERIFIED_TRANSACTION_CLAIM")


def completion_feedback(tools, reply=''):
    """Registered business work requires evidence; ordinary replies need no label."""
    if not getattr(tools, 'semantic_mode', False):
        return ''
    from .request_plan import pending
    missing=pending(tools)
    if missing and getattr(tools,'reply_metadata',{}).get('kind') not in ('clarification','explanation'):
        return '本轮已声明字段尚未齐全：'+','.join(missing)+'。继续提交这些字段，或针对缺项向买家确认；不得结束为已报价、已更新或付款指引。'
    if (not reply.startswith('[转人工]') and
            _affirmed(r'已(?:经)?(?:帮你|为你|给你)?(?:转人工|转接人工|呼叫人工)', reply)
            and not getattr(getattr(tools, 'flow_result', None), 'need_human', False)):
        return '尚未登记真实转人工请求，不能说已经转人工。确需人工时调用request_human_handoff；否则如实回复当前情况。'
    claim_feedback = _claim_feedback(tools, reply)
    if claim_feedback:
        return claim_feedback
    intent = getattr(tools, 'semantic_intent', None)
    if intent is None:
        metadata = getattr(tools, "reply_metadata", {})
        # A successful read-only policy lookup can support an ordinary answer
        # without another intent-label round. All body/transaction checks still run.
        if (not metadata.get('invalid_response_metadata') and not metadata.get('claims')
                and metadata.get('intent') in (None, 'other')
                and any(name == 'search_faq' and result.get('ok')
                        for name, result in getattr(tools, 'tool_results', []))):
            return ''
        if metadata.get("claims"):
            return ''
        if (metadata.get("intent") == "other" and not metadata.get("claims")
                and metadata.get("kind") in ("explanation", "acknowledgement", "clarification")):
            return ''
        return ('请说明本轮意图，最终回复必须返回JSON对象：' + RESPONSE_SCHEMA
                + '。普通说明或澄清可用other且claims=[]；涉及需求或交易时先记录意图并调用相应工具。')
    results = getattr(tools, 'tool_results', [])
    attempted = {name for name, _ in results}
    if tools.request_failure_reply():
        metadata = getattr(tools, 'reply_metadata', {})
        if metadata.get('kind') in ('explanation', 'clarification'):
            # References were checked above; assess still checks transaction
            # claims and waiting promises in the answer body.
            return ''
        return '本轮需求工具尚未成功；请修正参数或按工具缺项追问，不能说已保存或更新。'
    if intent == 'revise' and not attempted.intersection({
            'update_ticket_request', 'supplement_ticket_details', 'supplement_city',
            'confirm_previous_image', 'confirm_inquiry_purchase', 'clarify_ticket_request', 'begin_new_purchase', 'confirm_image_cinema', 'recover_image_context'}):
        return '已判断为补充或修改需求，但尚未执行对应工具。请调用工具取得结果，再回复；记录意图不等于已更新张数或报价。'
    if intent == 'inquire' and not attempted.intersection({'update_ticket_request', 'read_quote', 'supplement_ticket_details'}):
        return '已判断为询价，但尚未查询或计算当前需求。请调用相应报价工具；不要用旧订单金额回答。'
    if intent == 'reprice' and 'read_platform_order' not in attempted:
        return '尚未查询平台订单，请先调用read_platform_order，依据实际状态继续处理。'
    if (intent == 'reprice' and getattr(tools, 'platform_facts', {}).get('action') == 'REQUEST_REPRICE'
            and 'request_latest_quote_reprice' not in attempted):
        return '平台查询提示可申请改价，请使用查询返回的完整引用调用request_latest_quote_reprice；提交请求不等于成功。'
    return ''

def response_feedback(tools, text):
    """A synchronous query cannot leave an imaginary background job."""
    if not getattr(tools, 'semantic_mode', False):
        return ''
    if tools.flow_result is not None and tools.flow_result.pending_commands:
        return ''
    text = str(text or '')
    results = getattr(tools, 'tool_results', [])
    if text.startswith('[转人工]') and not re.search(r'人工|客服|真人|店主|老板', tools.text):
        queried = any(n != 'record_buyer_intent' for n, _ in results)
        if not queried:
            return '尚未查询事实或规则，不能只凭历史问题转人工。先核对本轮意图；普通咨询查询规则，缺项针对性追问，礼貌收尾直接回应。'
    if text.startswith('[转人工]') and results:
        failures = [(n, r) for n, r in results if not r.get('ok') and n != 'record_buyer_intent']
        if failures and all(r.get('status') in ('POLICY_NOT_FOUND', 'REPEATED_REJECTION')
                            or any(word in r.get('reason', '') for word in ('原话', '参数', '逐字', '意图', '格式', '不一致'))
                            for _, r in failures):
            return '当前只是参数、佐证或规则检索缺项，尚非人工才能处理。读取read_buyer_context或本店经验，修正工具参数；信息仍不明确就具体追问。不要复述内部工具错误。'
    metadata = getattr(tools, 'reply_metadata', {})
    if (metadata.get('kind') == 'clarification'
            and not metadata.get('claims')
            and not metadata.get('invalid_response_metadata')):
        # The Agent owns whether this is a question for the buyer. A valid
        # clarification may explain what can happen after missing input arrives;
        # do not reinterpret that as an already running background query.
        # assess() still validates references and all transaction/body claims.
        return ''
    waiting = re.search(r'稍等|等我|我.{0,12}(?:查一下|核一下|查好|核好|核对一下|核实一下)|(?:稍后|一会|回头).{0,8}(?:回复|告诉|通知)|有结果.{0,5}(?:同步|告诉|通知)', text)
    # Real order progress can legitimately require waiting; it cannot
    # justify promising an unstarted price lookup or a future message.
    if not waiting:
        return ''
    if '出票' in text and not re.search(r'查|核|稍后|有结果|等我', text):
        observations = [result for name, result in getattr(tools, 'tool_results', []) if name == 'read_order']
        if observations:
            rows = observations[-1].get('orders') or []
            if (observations[-1].get('ok') and len(rows) == 1
                    and rows[0].get('status') == 'PAID'
                    and rows[0].get('payment_state') == 'CONFIRMED'
                    and not rows[0].get('platform_shipped')):
                return ''
    pending = tools.session.image_context.get('awaiting_image_request') or tools.session.image_context.get('ticket_details') or {}
    return ('当前没有本轮已提交的后台查询任务，不能只承诺稍等或稍后回复。'
            '需要核价请现在调用工具；工具已返回缺项就直接追问，失败就说明具体原因。'
            '已保存需求：' + str(pending) + '；工具提示：' + getattr(tools, 'clarification_message', ''))

def unfinished_reply(tools):
    from .request_plan import pending
    from .request_observation import LABELS
    missing=pending(tools)
    if missing:
        return '本次补充已暂存，还需要确认：'+'、'.join(LABELS.get(k,k) for k in missing)+'；尚未重新报价，请勿按旧价付款。'
    if getattr(tools,'seller_context',{}).get('active'):
        return tools.seller_context['guidance']
    failed = tools.request_failure_reply()
    if failed:
        return failed
    from .quote_comparison import explanation
    comparison_reply=explanation(tools)
    if comparison_reply:
        return comparison_reply
    from .request_failure import pending_request
    pending = pending_request(tools)
    if pending:
        if pending.get('position_preference') and not pending.get('seats'):
            return ('本次位置偏好已保留：'+str(pending['position_preference'])+
                    '。具体位置和价格还未核实，请确认目标几排几座；不能沿用原位置报价。')
        missing = [label for key, label in [('cinema', '影院'), ('movie', '影片'),
                   ('date', '观影日期'), ('time', '开场时间')] if not pending.get(key)]
        if not pending.get('ticket_count') and not pending.get('seats'):
            missing.append('张数')
        if missing:
            return '已保留本次购票信息，还需要确认：'+'、'.join(missing)+'。'
        return '本次影片、日期、场次和张数已保留，请发对应场次截图，以便核对影院和实际价格。'
    return '本轮答复尚未完成核对，请联系店家继续确认。'

def failure_observation(tools, *, reference_tool=None):
    """Return the latest unresolved failure, preserving its structured cause."""
    intent = getattr(tools, 'semantic_intent', None)
    required = {'revise': {'update_ticket_request', 'supplement_ticket_details', 'supplement_city',
                            'confirm_previous_image', 'confirm_inquiry_purchase', 'clarify_ticket_request', 'begin_new_purchase', 'confirm_image_cinema', 'recover_image_context'},
                'inquire': {'update_ticket_request', 'read_quote', 'supplement_ticket_details'},
                'reprice': {'read_platform_order', 'request_latest_quote_reprice'}}.get(intent, set())
    if intent is None:
        # These reads are facts needed to answer the selected order/quote query.
        # Optional FAQ retrieval must not replace an otherwise valid answer.
        required = {'read_order', 'read_quote', 'read_platform_order', 'read_refund_status',
                    'get_purchase_guidance', 'read_ticket_progress'}
        if reference_tool is None and getattr(tools, 'reply_metadata', {}).get('kind') == 'acknowledgement':
            return None
    if reference_tool is not None:
        # A truthful failure reference is independent of which tools were
        # mandatory for this intent. Later successes still resolve old failures.
        required = {name for name, _ in getattr(tools, 'tool_results', [])}
    failed = []
    for name, result in getattr(tools, 'tool_results', []):
        if name not in required:
            continue
        if result.get('input_already_saved'):
            continue
        if result.get('status') == 'INTENT_REQUIRED' and intent in ('revise', 'inquire', 'reprice'):
            continue
        covered = set(result.get('request_fields') or [])
        if result.get('ok'):
            failed = [(n, r) for n, r in failed if not (
                (covered and r.get('request_fields') and set(r['request_fields']) <= covered)
                or (not covered and not r.get('request_fields') and n == name))]
        else:
            failed = [(n, r) for n, r in failed
                      if not (n == name and set(r.get('request_fields') or []) == covered)]
            failed.append((name, result))
    if reference_tool is not None:
        return next(((name,result) for name,result in reversed(failed) if name==reference_tool),None)
    if not failed:
        return None
    return failed[-1]


def _failure_needs_handoff(tools):
    observation = failure_observation(tools)
    if observation is None:
        return False
    result = observation[1]
    codes = {'PAID_ORDER_LOCKED', 'QUERY_UNKNOWN', 'ORDER_AMBIGUOUS'}
    return (result.get('status') in codes or
            (result.get('status') == 'BLOCKED' and result.get('reason_code') in codes))


def failure_reply(tools):
    """Explain a structured business failure without echoing adapter errors."""
    from .request_failure import explain
    observation = failure_observation(tools)
    if observation is None:
        return ''
    name, result = observation
    return explain(name, result)
