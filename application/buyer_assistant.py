"""Buyer-scoped semantic tools with guarded quoting and repricing requests."""

from datetime import datetime, timezone
import re

import templates
from application.tools import ReadOnlyTools, TOOL_SCHEMAS
from domain.conversation import ConvState
from domain.order.models import TERMINAL_STATES

CLOSED_STATES = set(TERMINAL_STATES) | {"CLOSED"}


def schema(name, description, properties=None):
    properties = properties or {}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


def explicit_ticket_count(text):
    """Only an affirmative, self-contained quantity; never extract arbitrary digits."""
    match = re.fullmatch(r"(?:要|我要|需要|买|改成|改为)?\s*([1-9][0-9]?|一|二|两|三|四|五|六|七|八|九|十)\s*张(?:票|电影票)?[。！!~～]?", str(text or "").strip())
    if not match:
        return None
    value = match.group(1)
    return int(value) if value.isdigit() else {"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10}[value]


def is_price_inquiry(text):
    return bool(re.search(r"多少钱|什么价|怎么卖|价格多少|几块|几元", str(text or "")))


def inquiry_quantity(text):
    # One grounded quantity only; alternatives and negation require clarification.
    text = str(text or "")
    if re.search(r"不|别|或者|还是|或", text):
        return None
    matches = re.findall(r"(?<![0-9一二两三四五六七八九十])([1-9][0-9]?|十|[一二两三四五六七八九])\s*张", text)
    if len(matches) != 1:
        return None
    return explicit_ticket_count(matches[0] + "张")


class BuyerAssistantTools(ReadOnlyTools):
    prefer_agent = True
    schemas = TOOL_SCHEMAS + [
        schema('clarify_ticket_request', '买家否定当前座位或场次，但本轮没有完整的新位置时调用。暂停当前报价并追问，不修改已拍订单。',
               {'evidence': {'type': 'string'}}),
        schema('supplement_ticket_details', '保存本轮明确提供的影院、影片、日期、时间或座位，包括只发影院名、下一句今天、尚未发图。只提交本轮原文字段，历史已存字段由工具跨轮合并；未提及字段填空。不猜日期或片名，不执行改价。',
               {k:{'type':'string','description':v} for k,v in {'evidence':'本轮完整原话','movie':'原话中的影片名','date':'今天、明天、后天或YYYY-MM-DD原文','time':'原文开场时间，例如2230、9:15或晚上9:15，不自行改写为规范时间','seats':'原文明确座号，例如六排十座十一座；区域偏好放position_preference','position_preference':'本轮原文位置偏好，例如8排最中间、灰色的部分；不编造具体座号','cinema':'本轮明确提供的影院名；历史已保存的影院不要重复填入'}.items()}),
        schema("quote_ticket_inquiry", "仅万达买家问几张多少钱、六排四张多少钱时调用。按最新场次报价计算询价参考金额，保留排数需求；不修改订单、不确认座位、不生成可交易新报价。", {"evidence": {"type": "string", "description": "本轮买家完整原话"}}),
        schema("read_platform_order", "查询当前买家已关联订单的平台实时状态和金额，并核对最新报价。问改价、是否拍下、付款或订单进度时必须调用；查询不到不等于没拍下。"),
        schema("request_latest_quote_reprice", "买家要求改价或确认替换旧订单时，先调用read_platform_order，再原样使用返回的order_no及quote_reference。金额由代码读取报价。已付、多单、版本变化或结果未知拒绝执行；REQUESTED不等于成功。", {
            "order_no": {"type": "string"}, "quote_id": {"type": "string"},
            "demand_id": {"type": "string"}, "demand_rev": {"type": "integer"}}),
        schema("supplement_ticket_count", "买家明确补充截图购票张数时调用；提交本轮原话中的完整肯定张数短语，例如要两张。不下单不付款。",
               {"evidence": {"type": "string", "description": "本轮原话中表达张数的短语"}}),
        schema(
            "get_purchase_guidance",
            "买家问下单方式，或报价后确认接受购票信息时调用。结合真实订单和报价说明下一步；确认选择不代表已经拍下或授权改价，不能用旧聊天猜订单状态。",
        ),
        schema(
            "supplement_city",
            "买家在上一张截图后补充或纠正城市时调用。相同城市且已有有效报价时只确认并保留报价，不重复识别发送；更换城市或尚未核实时使用已有截图重新匹配，不下单不付款。城市必须逐字来自本轮买家原话。",
            {"city": {"type": "string", "description": "本轮买家明确说出的城市名"}},
        ),
    ]

    def __init__(self, service, session, buyer_text, cfg, faq_lookup):
        super().__init__(faq_source=faq_lookup)
        self.service, self.session, self.text, self.cfg = (
            service,
            session,
            buyer_text,
            cfg,
        )
        self.conv = session.conversation
        self.identity = {
            k: str(getattr(self.conv, k, "") or "")
            for k in ("tenant_id", "shop_id", "buyer_id", "chat_id")
        }
        self.image_snapshot = dict(session.image_context or {})
        self.flow_result = None
        self.city_attempted = False

    def handlers(self):
        return dict(
            super().handlers(),
            update_ticket_request=self.update_ticket_request,
            confirm_previous_image=self.confirm_previous_image,
            record_buyer_intent=self.record_buyer_intent,
            begin_new_inquiry=self.begin_new_inquiry,
            begin_new_purchase=self.begin_new_purchase,
            read_ticket_progress=self.read_ticket_progress,
            confirm_image_cinema=self.confirm_image_cinema,
            recover_image_context=self.recover_image_context,
            confirm_inquiry_replacement=self.confirm_inquiry_replacement,
            confirm_inquiry_purchase=self.confirm_inquiry_purchase,
            read_platform_order=self.read_platform_order,
            request_latest_quote_reprice=self.request_latest_quote_reprice,
            get_purchase_guidance=self.get_purchase_guidance,
            quote_ticket_inquiry=self.quote_ticket_inquiry,
            supplement_city=self.supplement_city,
            supplement_ticket_count=self.supplement_ticket_count,
            supplement_ticket_details=self.supplement_ticket_details,
            clarify_ticket_request=self.clarify_ticket_request,
            read_buyer_context=self.read_buyer_context,
            request_human_handoff=self.request_human_handoff,
        )

    def update_ticket_request(self, intent, quantity, quantity_text, evidence):
        from .semantic_request import update
        return update(self, intent, quantity, quantity_text, evidence)

    def clarify_ticket_request(self, evidence, invalidated_fields=None):
        from .request_state import clarify
        return clarify(self,evidence,invalidated_fields)

    def demand_context(self):
        """Compatibility serialization backed by the shared context assembler."""
        import json
        from .assistant_context import build
        return json.dumps(build(self)["demand"], ensure_ascii=False, default=str)

    def enable_semantic_routing(self):
        from copy import deepcopy
        from .semantic_request import SCHEMA
        self.semantic_mode = True
        # One declared field plan is the only agent-side staging mechanism.
        self.schemas = deepcopy([s for s in type(self).schemas if s['function']['name'] not in
                        ('supplement_ticket_count', 'quote_ticket_inquiry', 'complete_ticket_request')] + [SCHEMA])
        self.schemas.append(schema('confirm_previous_image',
            '最新图不完整且买家明确确认沿用上一场时调用。只能确认工具上下文中已有的上一场，不能自动认定两张图属于同场。',
            {'evidence': {'type': 'string', 'description': '本轮完整原话'}}))
        self.schemas.append(schema('record_buyer_intent',
            '调用写工具前先记录本轮语义：inquire仅问价格，revise明确修改购票需求，reprice明确要求修改订单价格或用已确认新报价替换旧单，other咨询/含糊。不得把多少钱、是否便宜、是否能改当成改价许可。',
            {'intent': {'type': 'string', 'enum': ['inquire', 'revise', 'reprice', 'other']},
             'evidence': {'type': 'string', 'enum': [self.text], 'description': '只使用本轮原话，不拼接历史消息'}}))
        self.schemas.append(schema('begin_new_inquiry',
            '买家明确另外咨询其他场次且不修改原订单时调用。建立独立询价需求，保留原订单；随后补齐影片日期时间等。独立询价不可直接改原订单。',
            {'evidence': {'type': 'string', 'description': '本轮完整原话'}}))
        self.schemas.append(schema('begin_new_purchase',
            '买家明确另外购买、再买一张等新增购买时使用，先记录revise。与改原单张数不同：新建独立需求，保留旧订单；本轮张数有原话佐证，接着发的截图会继承该张数。',
            {'evidence': {'type': 'string'}, 'quantity': {'type': 'integer'}, 'quantity_text': {'type': 'string'}}))
        self.schemas.append(schema('read_ticket_progress',
            '买家催出票、要取票码、问可以了吗或退款后是否重拍时，查询真实当前订单的付款/关闭/发货和取票信息；查询本身不创建、补发或退款。已发货不等于已核实二维码。'))
        self.schemas.append(schema('confirm_image_cinema',
            '已向买家展示历史图片的影院候选，买家本轮明确确认仍是这家时调用，先记录revise。仅确认上下文里对应source_message_id的影院；否定或换影院不得调用。随后重新核对当前场次，不沿用历史售价。',
            {'evidence': {'type': 'string'}, 'source_message_id': {'type': 'string'}}))
        self.schemas.append(schema('recover_image_context',
            '仅恢复已有图片中的影院和场次，不保存本轮文字字段。本轮明确提供几排几座时必须使用supplement_ticket_details的seats字段，不能用本工具代替保存文字座位。仅指代图片位置、没有明确文字座号时，先记录revise，再恢复图片。无图或数字含义不清时针对性追问。历史影院归属不明时仅展示候选待确认。'))
        self.schemas.append(schema('confirm_inquiry_replacement',
            '独立询价后，买家明确要求用该需求替换原未付款订单时调用。必须先查询平台并记录reprice意图。重新核价，不能直接把询价当成改价成功。',
            {'evidence': {'type': 'string'}, 'order_no': {'type': 'string'}}))
        self.schemas.append(schema('confirm_inquiry_purchase',
            '买家确认购买独立询价且没有其他未完结订单时调用，先记录revise意图。重新核价生成可拍下报价；已有原未付款订单应明确使用替换工具。',
            {'evidence': {'type': 'string'}}))
        self.schemas.append(schema('read_buyer_context',
            '读取当前会话买家近期原话和已保存需求。指代含糊或历史佐证被拒时查询；已保存字段直接沿用，不重新抄为本轮原话。历史文本仅供理解，不构成改价、付款或选座成功证据。'))
        self.schemas.append(schema('request_human_handoff',
            '仅在买家明确请求转接人工，或工具已确认订单问题确需人工时，登记请求。询问身份（如“是人工吗”“你是机器人吗”）不是请求转接，禁止调用本工具，应该如实回答店铺自动回复。普通咨询、参数错误、缺项先查工具或追问。工具不改价、不下单、不退款，也不表示人工已接通。'))
        from .buyer_evidence import FIELDS as reference_fields
        for spec in self.schemas:
            params = spec['function']['parameters']
            if spec['function']['name']=='clarify_ticket_request':
                params['properties']['invalidated_fields']={'type':'array','minItems':1,'uniqueItems':True,
                    'items':{'type':'string','enum':['ticket_count','seats','movie','date','time','cinema','city','position_preference']},
                    'description':'本轮已否定或尚未确定的字段。只换座位填seats；换场次时间未定填time，不将已撤回值作为事实继承。'}
            if spec['function']['name'] in {'update_ticket_request','supplement_ticket_details','supplement_city'}:
                params['properties']['request_fields'] = {'type':'array','uniqueItems':True,
                    'items':{'type':'string','enum':['ticket_count','seats','movie','date','time','cinema','city','position_preference']},
                    'description':'首次需求调用必须声明本轮全部需要保存或变更的字段，后续调用沿用同一集合。所有声明字段到齐前不会核价。'}
                params['properties']['field_evidence']={'type':'object','additionalProperties':False,
                    'properties':{k:{'type':'string'} for k in ('ticket_count','seats','movie','date','time','cinema','city','position_preference')},
                    'description':'首次声明全部字段的本轮原文片段，即使稍后才提交该值也先提供片段。不能声明本轮没有提出的字段；历史未变字段不必声明。'}
            if spec['function']['name'] == 'supplement_ticket_details':
                params['properties']['source_refs'] = {'type': 'object', 'additionalProperties': False,
                    'properties': {key: {'type': 'string'} for key in reference_fields},
                    'description': '历史已明确但尚未保存的字段，可引用read_buyer_context返回can_reference=true的message_ref；按字段映射，值仍逐字来自被引用买家消息。不能引用跨需求或未知时间消息。'}
            if 'evidence' in params.get('properties', {}):
                params['required'] = [key for key in params.get('required', []) if key != 'evidence']
                params['properties']['evidence']['description'] = '建议省略：代码自动绑定本轮原话；如提交，必须与本轮原话一致，禁止拼接历史。'

    def read_buyer_context(self):
        from .assistant_context import build
        snapshot = build(self)
        return {'ok': True, 'current_message': self.text,
                'buyer_messages': snapshot['buyer_message_sources'],
                'saved_request': snapshot['demand'], 'context': snapshot,
                'next_actions': ['已保存字段由工具合并；明确历史字段用source_refs引用can_reference=true的消息；指代、选择或来源仍不明确时针对性确认。']}

    def search_faq(self, keyword):
        result = super().search_faq(keyword)
        experience = str(self.cfg.assistant.experience or '').strip()
        if experience:
            result = dict(result, ok=True, status='SHOP_POLICY_AVAILABLE', shop_experience=experience,
                          note='只采用与问题相关的已配置规则；未覆盖的问题仍需说明未知，不推导价格或执行成功。')
        return result

    def confirm_inquiry_purchase(self, evidence):
        if (evidence != self.text or getattr(self, 'semantic_intent', None) != 'revise'
                or not getattr(self.conv, 'demand_inquiry', False)):
            return {'ok': False, 'reason': '需要买家明确确认购买当前独立询价。'}
        if any(o.state not in CLOSED_STATES for o in self._orders()):
            return {'ok': False, 'reason': '已有未完结订单，先确认是否替换原单，不引导重复拍下。'}
        self.conv.requested_order_no = ''
        return self._requote_inquiry(evidence)

    def confirm_inquiry_replacement(self, evidence, order_no):
        if (evidence != self.text or getattr(self, 'semantic_intent', None) != 'reprice'
                or not getattr(self.conv, 'demand_inquiry', False)):
            return {'ok': False, 'reason': '需要明确确认用当前询价替换原订单。'}
        facts = self.read_platform_order()
        if not facts.get('ok') or not facts.get('unpaid') or facts.get('order_no') != order_no:
            return {'ok': False, 'reason': '原订单尚未唯一核实为未付款，不替换。'}
        self.conv.requested_order_no = order_no
        return self._requote_inquiry(evidence)

    def _requote_inquiry(self, evidence):
        self.conv.demand_inquiry = False
        ctx = self.session.image_context
        try:
            if ctx.get('ticket_details'):
                from .ticket_details import supplement
                count = (ctx['ticket_details'].get('ticket_count')
                         or len(ctx['ticket_details'].get('seats') or []))
                result = supplement(self, evidence, ticket_count=count)
            else:
                result = self._resume_quote(ticket_count=ctx.get('ticket_count'))
            if not result.get('ok') or not self.conv.quote_id:
                self.conv.demand_inquiry = True
            else:
                ctx['inquiry_only'] = False
            return result
        except Exception:
            self.conv.demand_inquiry = True
            raise

    def begin_new_inquiry(self, evidence):
        from copy import deepcopy
        from uuid import uuid4
        if evidence != self.text or getattr(self, 'semantic_intent', None) != 'inquire':
            return {'ok': False, 'reason': '须先记录本轮独立询价意图及原话。'}
        if getattr(self, 'new_inquiry_started', False):
            return {'ok': True, 'reason': '本轮已建立独立询价，请补齐信息。'}
        self.new_inquiry_started = True
        ctx = self.session.image_context
        archive = list(ctx.get('demand_archive') or [])
        archive.append({'demand_id': getattr(self.conv, 'demand_id', ''),
                        'demand_rev': getattr(self.conv, 'demand_rev', 0),
                        'quote_id': getattr(self.conv, 'quote_id', ''),
                        'ticket_details': deepcopy(ctx.get('ticket_details') or {}),
                        'awaiting_image_request': deepcopy(ctx.get('awaiting_image_request') or {})})
        ctx['demand_archive'] = archive[-20:]
        ctx['ticket_details'] = {'pending': True, 'awaiting': 'details'}
        ctx['inquiry_only'] = True
        ctx['inquiry_started_at'] = datetime.now(timezone.utc).isoformat()
        ctx.pop('awaiting_image_request', None)
        self.conv.demand_id, self.conv.demand_rev = str(uuid4()), 0
        self.conv.demand_inquiry = True
        self.conv.requested_order_no = ''
        self.conv.quote = None; self.conv.quote_id = ''; self.conv.quote_kind = ''
        self.conv.state = ConvState.IDLE
        return {'ok': True, 'status': 'NEW_INQUIRY', 'demand_id': self.conv.demand_id,
                'reason': '已建立独立询价。原订单和报价绑定保留，请补齐本次影片、日期、开场时间及张数。'}

    def recover_image_context(self):
        if getattr(self, 'semantic_intent', None) != 'revise':
            return {'ok': False, 'status': 'INTENT_REQUIRED', 'reason': '先确认买家是在补充当前截图需求，不能把其他咨询当成重新报价。'}
        if not self.session.image_context.get('url'):
            return {'ok': True, 'status': 'NEEDS_INFORMATION',
                    'reason': '当前没有待恢复的截图；没有执行识别或核价。',
                    'missing_fields': ['可核对的截图或明确的购票信息'],
                    'next_actions': ['结合本轮原话针对性追问。数字含义不明时先确认其含义，不补造排号、座号或张数，不要求重发已知信息。']}
        return self._resume_quote(ticket_count=self.session.image_context.get('ticket_count'))

    def begin_new_purchase(self, evidence, quantity, quantity_text):
        from .purchase_continuation import begin
        return begin(self, evidence, quantity, quantity_text)

    def read_ticket_progress(self):
        from .purchase_continuation import progress
        from .ticket_flow import FlowResult
        result = progress(self)
        self.progress_facts = result
        self.safe_reply_projection = result['guidance']
        self.flow_result = FlowResult(ok=bool(result.get('ok')), state=self.conv.state,
            channel=self.conv.channel, messages=[result['guidance']], template_keys=['ORDER_PROGRESS'])
        return result

    def confirm_image_cinema(self, evidence, source_message_id):
        from .image_history import confirm
        return confirm(self, evidence, source_message_id)

    def record_buyer_intent(self, intent, evidence):
        if evidence != self.text or intent not in ('inquire', 'revise', 'reprice', 'other'):
            return {'ok': False, 'reason': '意图必须绑定本轮完整原话，不使用历史消息。',
                    'expected_evidence': self.text, 'allowed_intents': ['inquire','revise','reprice','other']}
        current = getattr(self, 'semantic_intent', None)
        if current is not None and current != intent:
            changed = any(r.get('ok') and n in {'update_ticket_request', 'supplement_ticket_details',
                          'supplement_city', 'begin_new_inquiry', 'clarify_ticket_request',
                          'request_latest_quote_reprice', 'confirm_previous_image',
                          'confirm_inquiry_purchase', 'confirm_inquiry_replacement'}
                          for n, r in getattr(self, 'tool_results', []))
            if changed or not (current == 'other' and intent in ('revise', 'inquire')
                               or current in ('revise', 'inquire') and intent == 'other'):
                return {'ok': False, 'reason': '本轮已记录其他意图，不能将询价升级为改价或确认购买。请向买家澄清。'}
        self.semantic_intent = intent
        result = {'ok': True, 'intent': intent, 'evidence': evidence}
        if intent == 'other':
            result['reply_scope'] = ('只回答本轮原话，不接续历史中的核查或转人工任务。'
                                     '若只是礼貌收尾，简短回应收到即可；没有实际待执行任务，不承诺稍等或有结果再同步。')
        return result

    def execute(self, name, args):
        if getattr(self, 'semantic_mode', False) and name not in {
                s['function']['name'] for s in self.schemas}:
            from .tool_observation import normalize
            return normalize(name, {'ok': False, 'reason': '请使用本轮提供的工具及参数。'}, self)
        args = dict(args or {})
        if getattr(self, 'semantic_mode', False):
            spec = next((s['function'] for s in self.schemas if s['function']['name'] == name), {})
            if 'evidence' in spec.get('parameters', {}).get('properties', {}):
                args.setdefault('evidence', self.text)
                if ' '.join(str(args['evidence']).split()) == ' '.join(self.text.split()):
                    args['evidence'] = self.text
        previous_operations = {c.command_id for c in getattr(self.flow_result, "pending_commands", [])}
        result = None
        if getattr(self,'request_plan_enabled',False):
            from .request_plan import stage
            try:
                result=stage(self,name,args)
            except Exception as exc:
                result={'ok':False,'status':'QUERY_UNKNOWN','reason':'本轮需求暂存失败：'+type(exc).__name__}
        if result is None:
            args.pop('request_fields',None)
            args.pop('field_evidence',None)
            result = super().execute(name, args)
        else:
            self.calls.append((name,dict(args)))
        if result.get('ok') and name in {'begin_new_purchase','begin_new_inquiry','clarify_ticket_request'}:
            self.request_plan=None
            self.session.image_context.pop('agent_request_plan',None)
        self.observed_tools = getattr(self, 'observed_tools', set()) | {name}
        if (getattr(self.conv, 'demand_inquiry', False) and self.flow_result is not None
                and self.flow_result.ok and self.conv.quote_id and self.service.quote_store):
            q = self.service.quote_store.get(self.conv.quote_id)
            if q and q.metadata.get('inquiry_only'):
                self.flow_result.messages = [
                    f'{q.cinema}｜{q.movie}｜{q.show}\n'
                    f'{q.ticket_count}张，单价{q.unit_sell_price_fen / 100:.2f}元，'
                    f'合计{q.total_sell_price_fen / 100:.2f}元。本次为独立询价，原订单未修改。']
                self.flow_result.template_keys = ['INDEPENDENT_INQUIRY']
        if (getattr(self, 'semantic_mode', False) and self.flow_result is not None
                and self.flow_result.ok and not self.flow_result.pending_commands
                and not self.flow_result.need_human and self.flow_result.template_keys
                and set(self.flow_result.template_keys) <= {'TICKET_DETAILS', 'NEW_PURCHASE'}):
            from .request_observation import project
            observation = dict(code=(self.flow_result.template_keys or ['NEEDS_INFORMATION'])[0],
                               source_message_id=result.get('source_message_id'),
                               cinema_candidate=self.session.image_context.get('cinema_candidate'))
            result = dict(result, **project(self, True, observation=observation))
            self.flow_result = None
        if (getattr(self, 'semantic_mode', False) and self.flow_result is not None
                and not self.flow_result.ok and not self.flow_result.need_human
                and not self.flow_result.pending_commands):
            # A business rejection is an observation for the agent, not a
            # terminal prewritten customer reply. Quote/delivery cards and
            # actual operations keep their existing guarded terminal path.
            flow=self.flow_result
            result=dict(result,ok=False,status=result.get('status') or 'BUSINESS_CHECK_FAILED',
                source='local_flow',public_reason='\n'.join(flow.messages),
                reason='\n'.join(flow.messages),business_codes=list(flow.template_keys),
                next_actions=['EXPLAIN_ACTUAL_FAILURE_OR_ASK_MISSING_FIELDS'])
            self.flow_result = None
        from .tool_observation import normalize
        created = tuple(c.command_id for c in getattr(self.flow_result, "pending_commands", [])
                        if c.command_id not in previous_operations)
        result = normalize(name, result, self, operation_ids=created)
        if name in {'update_ticket_request', 'supplement_ticket_details', 'supplement_city'}:
            fields = {k for k in ('cinema', 'movie', 'date', 'time', 'seats', 'position_preference') if args.get(k)}
            if args.get('quantity') is not None or args.get('ticket_count') is not None:
                fields.add('ticket_count')
            if name == 'supplement_city':
                fields.add('city')
            result['request_fields'] = sorted(fields)
            if not result.get('ok') and result.get('status') == 'INVALID_FIELD_EVIDENCE':
                from .request_failure import already_saved_fields
                remaining = fields - already_saved_fields(self, args)
                result['request_fields'] = sorted(remaining)
                result['input_already_saved'] = bool(fields and not remaining)
        self.tool_results = getattr(self, 'tool_results', []) + [(name, result)]
        return result

    def finalization_feedback(self, reply=''):
        from .assistant_reply import completion_feedback
        return completion_feedback(self, reply)

    def reply_feedback(self, text):
        from .assistant_reply import response_feedback
        return response_feedback(self, text)

    def unfinished_reply(self):
        from .assistant_reply import unfinished_reply
        return unfinished_reply(self)

    def request_failure_reply(self):
        from .assistant_reply import failure_reply
        return failure_reply(self)

    def confirm_previous_image(self, evidence):
        if evidence != self.text:
            return {'ok': False, 'reason': '必须提供本轮完整原话。'}
        ctx = self.session.image_context
        if not ctx.get('verified') or not ctx.get('unverified_image'):
            return {'ok': False, 'reason': '没有等待确认的上一场图片。'}
        pending = ctx.pop('unverified_image')
        self.image_snapshot = dict(ctx)
        result = self._resume_quote(ticket_count=ctx.get('ticket_count'))
        if not result.get('ok'):
            ctx['unverified_image'] = pending
        return result

    def supplement_ticket_details(self, evidence, movie='', date='', time='', seats='', cinema='', source_refs=None, position_preference=''):
        if getattr(self, 'semantic_mode', False):
            intent = getattr(self, 'semantic_intent', None)
            if intent not in ('revise', 'reprice', 'inquire'):
                return {'ok': False, 'status': 'INTENT_REQUIRED', 'reason': '补充场次前先记录买家是在修改需求还是另行询价。'}
            if intent == 'inquire' and not getattr(self.conv, 'demand_inquiry', False):
                return {'ok': False, 'reason': '本轮是新场次询价，请先建立独立询价，不覆盖原需求。'}
        if getattr(self, 'semantic_mode', False) and self.session.image_context.get('unverified_image'):
            from .schedule_image import recover_pending
            recover_pending(self.service, self.session, self.cfg)
        from .ticket_details import supplement
        from .buyer_evidence import resolve
        try:
            referenced = resolve(self, dict(movie=movie,date=date,time=time,seats=seats,cinema=cinema,position_preference=position_preference), source_refs)
        except ValueError as exc:
            return {'ok': False, 'status': 'INVALID_SOURCE_REFERENCE', 'reason': str(exc)}
        self.referenced_fields = referenced
        try:
            return supplement(self, evidence, movie, date, time, seats, cinema,
                              position_preference=position_preference)
        finally:
            self.referenced_fields = {}

    def request_human_handoff(self):
        from .ticket_flow import FlowResult
        if not all(self.identity.values()):
            return {'ok': False, 'status': 'INVALID_FIELD_EVIDENCE'}
        self.flow_result = FlowResult(ok=True, need_human=True, state=self.conv.state,
            channel=self.conv.channel, messages=['已登记转人工请求，请店家接手处理。'],
            template_keys=['AI_HANDOFF'])
        return {'ok': True, 'status': 'HANDOFF_REQUESTED', 'guidance': '已登记请求，不代表人工已经接通。'}

    def read_platform_order(self):
        from .purchase_actions import inspect
        self.platform_facts = inspect(self)
        return self.platform_facts

    def request_latest_quote_reprice(self, order_no=None, quote_id=None, demand_id=None, demand_rev=None):
        if getattr(self.conv, 'demand_inquiry', False):
            return {'ok': False, 'status': 'INQUIRY_ONLY', 'reason': '当前为独立询价，不能替换原订单。请确认购买及订单归属。'}
        if getattr(self, 'semantic_mode', False) and getattr(self, 'semantic_intent', None) != 'reprice':
            return {'ok': False, 'status': 'INTENT_REQUIRED',
                    'reason': '没有本轮明确改价意图。请先理解并记录买家意图；询价或含糊请求不得申请改价。'}
        if getattr(self, 'semantic_mode', False) and any(v is None for v in (order_no, quote_id, demand_id, demand_rev)):
            return {'ok': False, 'status': 'REFERENCE_REQUIRED',
                    'reason': '先查询平台订单，再提交返回的订单号、报价及需求版本。'}
        if getattr(self, 'semantic_intent', None) == 'inquire':
            return {'ok': False, 'reason': '本轮是询价，不允许改价。'}
        if not getattr(self, 'semantic_mode', False) and self.conv.channel == "wanda" and is_price_inquiry(self.text):
            return self.quote_ticket_inquiry(self.text)
        from .purchase_actions import request_reprice
        return request_reprice(self, order_no=order_no, quote_id=quote_id,
                               demand_id=demand_id, demand_rev=demand_rev)

    def _orders(self):
        if not all(self.identity.values()) or self.service.order_store is None:
            raise ValueError("订单身份或订单查询服务不完整，不能判定尚未下单")
        return self.service.order_store.by_identity(self.identity)

    def read_order(self):
        rows = self._orders()
        # Explicit projection: never disclose supplier credentials, cost or raw payload.
        return {
            "ok": True,
            "source": "已保存订单及平台回执",
            "platform_orders": [
                {'order_no': r['platform_order_id'], 'status': r['platform_status'],
                 'amount_yuan': (r.get('amount_fen') or 0)/100,
                 'paid_at': r.get('paid_at'), 'checked_at': r.get('checked_at'),
                 'platform_shipped': r['platform_status'] in (3, 4),
                 'sync_state': r['sync_state'], 'quote_bound': False,
                 'guidance': '平台订单已记录，购票信息尚未绑定；不能据此改价、出票或重复要求下单付款。'}
                for r in (self.service.order_store.platforms.by_identity(self.identity)
                          if hasattr(self.service.order_store, 'platforms') else [])
                if r['platform_order_id'] not in {o.platform_order_id or o.provider_order_id for o in rows}
            ],
            "orders": [
                {
                    "order_no": o.provider_order_id or o.order_id,
                    "quote_id": o.quote_id,
                    "demand_id": o.demand_id,
                    "demand_rev": o.demand_rev,
                    "platform_order_exists": bool(o.provider_order_id),
                    "status": o.state,
                    "payment_state": o.payment_state,
                    "platform_shipped": bool(o.shipped),
                    "ticket_image_available": bool(o.payload.get('wanda_ticket_image')),
                    "cinema": o.cinema,
                    "movie": o.movie,
                    "start_time": o.show,
                    "seats": o.seats,
                    "total_yuan": f"{o.amount_fen / 100:.2f}",
                    "reprice_unconfirmed": bool(
                        (o.payload or {}).get("quote_revision_pending")
                    ),
                }
                for o in rows[:5]
            ],
        }

    def read_quote(self):
        if not all(self.identity.values()) or self.service.quote_store is None:
            return {"ok": False, "reason": "报价身份或查询服务不完整"}
        rows = self.service.quote_store.by_identity(self.identity)
        from .quote_comparison import lookup
        comparison=lookup(self)
        if comparison:
            return {'ok':True,'status':'SELF_BUY_RECOMMENDED','quote':None,
                    'comparison':comparison,'tradable':False,
                    'reason':comparison['reason'],
                    'next_actions':['EXPLAIN_COMPARISON_DO_NOT_REQUEST_SAME_IMAGE'],
                    'guidance':'已识别影院、场次和座位；本次价格比较后建议自购。按比价观察解释规则售价、比较基准及来源，'
                               '不能说未发截图，不能承诺按该记录改价、下单或付款。价格需以重新核价为准。'}
        if not rows:
            return {"ok": True, "quote": None, "reason": "尚未生成报价，需要选座截图"}
        q = rows[0]
        from .request_plan import has_pending
        pending = has_pending(self.conv,self.session.image_context,self.cfg.assistant.memory_hours)
        same_demand = (not getattr(self.conv, 'demand_id', '') or
                       (q.demand_id == self.conv.demand_id and q.demand_rev == self.conv.demand_rev))
        valid = q.is_usable() and same_demand and not pending
        tradable = bool(
            valid and q.is_order_ready()
            and (q.provider == "WANDA" or q.expires_at)
            and q.ticket_count > 0
            and q.total_sell_price_fen > 0
        )
        return {
            "ok": True,
            "quote": {
                "valid": valid,
                "quote_id": q.quote_id,
                "demand_id": q.demand_id,
                "demand_rev": q.demand_rev,
                "purchase_ready": tradable,
                "tradable": tradable,
                "cinema": q.cinema,
                "movie": q.movie,
                "start_time": q.show,
                "seats": q.selected_seats,
                "quote_kind": q.metadata.get("quote_kind", "ORDER_TOTAL"),
                "total_yuan": f"{q.total_sell_price_fen / 100:.2f}",
                "unit_yuan": f"{q.unit_sell_price_fen / 100:.2f}",
                "seat_count": q.ticket_count,
            },
        }

    def read_refund_status(self):
        rows = self._orders()
        return {
            "ok": True,
            "orders": [
                {
                    "order_no": o.provider_order_id or o.order_id,
                    "status": o.state,
                    "payment_state": o.payment_state,
                }
                for o in rows[:5]
            ],
            "note": "仅有已保存状态；没有退款到账证据时不得声称已到账。",
        }

    def review_reply(self, text):
        from .assistant_reply import assess
        return assess(self, text)

    def guard_reply(self, text):
        """Compatibility entry: all reply checks use the same policy."""
        if not getattr(self, 'semantic_mode', False) and self.conv.channel == "wanda" and is_price_inquiry(self.text):
            return self.quote_ticket_inquiry(self.text)["guidance"]
        return self.review_reply(text).text

    def get_purchase_guidance(self):
        rows = self._orders()
        from .request_plan import has_pending
        demand_pending=has_pending(self.conv,self.session.image_context,self.cfg.assistant.memory_hours)
        if getattr(self.conv, 'demand_id', '') or self.conv.quote_id:
            from .assistant_reply import _is_current_order
            current = [o for o in rows if _is_current_order(self, {
                'demand_id': o.demand_id, 'demand_rev': o.demand_rev, 'quote_id': o.quote_id})]
            if rows and not current:
                return {'ok': True, 'payment_blocked': True, 'status': 'NO_CURRENT_ORDER',
                        'guidance': '本次需求尚未关联到对应订单，不能使用其他需求的订单金额。可以继续核对本次场次和价格；付款或改价前仍需确认订单与有效报价。'}
            rows = current
        active = [o for o in rows if o.state not in CLOSED_STATES]
        placed = [o for o in active if o.provider_order_id]
        if len(placed) > 1:
            return {
                "ok": True,
                "payment_blocked": True,
                "guidance": "查到您有多笔订单，您要咨询的是哪一笔呢？",
            }
        if demand_pending and not (placed and (placed[0].shipped or placed[0].payment_state!='UNPAID')):
            return {'ok': True, 'payment_blocked': True, 'status': 'DEMAND_PENDING',
                    'guidance': '本次购票需求有变更，位置或场次尚待核实，原报价不能作为新需求的已确认价格。请先补齐当前缺项，核价完成前不要付款。'}
        if placed:
            o = placed[0]
            if o.shipped:
                return {'ok': True, 'payment_blocked': True,
                        'guidance': '这笔订单已发货，无需再次下单或付款。', 'order': self.read_order()}
            if o.payment_state != "UNPAID":
                return {
                    "ok": True,
                    "payment_blocked": True,
                    "guidance": ("这笔订单已确认付款，无需再次下单或付款。" if o.payment_state == 'CONFIRMED'
                                 else "这笔订单的付款状态还需要核实，请暂时不要重复下单或付款。"),
                    "order": self.read_order(),
                }
            quotes = (self.service.quote_store.by_identity(self.identity)
                      if self.service.quote_store is not None else [])
            latest = quotes[0] if quotes else None
            if latest is None or (latest.quote_id != o.quote_id
                                  or latest.provider != o.provider
                                  or latest.total_sell_price_fen != o.amount_fen):
                current = f"已记录的原订单金额为 {o.amount_fen / 100:.2f} 元。"
                if latest is not None and latest.is_order_ready():
                    current += f"最新报价为 {latest.total_sell_price_fen / 100:.2f} 元；原订单尚未确认更新为这次报价。"
                else:
                    current += "最新整单报价或改价结果尚未核实。"
                return {"ok": True, "payment_blocked": True,
                        "next_tool": "read_platform_order",
                        "guidance": current + "请先不要付款或重复下单，需要先查询平台确认是否可以按最新报价处理。"}
            if (o.payload or {}).get(
                "quote_revision_pending"
            ) or o.state == "REPRICE_PENDING":
                return {
                    "ok": True,
                    "payment_blocked": True,
                    "guidance": "已有原订单，改价尚未确认完成；请先不要付款或重复下单。",
                }
            return {
                "ok": True,
                "guidance": "已有待付款订单，查看原订单；核对影院、场次、座位和最终金额，确认改价完成后再付款。不要再次点击购买。",
                "order": self.read_order(),
            }
        from .purchase_actions import purchase_state_blocked
        if purchase_state_blocked(self.conv):
            return {
                "ok": False,
                "reason": "会话进度显示可能已有订单，但订单记录不完整，请人工核对，不要重复下单。",
            }
        quote = self.read_quote()
        if not quote.get("ok"):
            return quote
        if (quote.get("quote") or {}).get("quote_kind") == "UNIT_PRICE":
            return {"ok": True, "payment_blocked": True, "guidance": "已经报过单张价格，请确认需要几张票；系统生成整单报价后再引导拍下或改价。"}
        if not (quote.get("quote") or {}).get("purchase_ready"):
            return {
                "ok": True,
                "payment_blocked": True,
                "guidance": "先发选座截图或补齐上一张截图缺少的信息，报价确认后再拍下。",
            }
        return {
            "ok": True,
            "status": "READY_TO_PURCHASE",
            "purchase_ready": True,
            "guidance": "本次整单报价有效。若买家正在确认购票，可承接确认并说明尚未拍下时可以拍下；若已拍下，请勿重复下单，等待对应订单同步或用订单卡片核对。尚未核验到平台订单不能说已改价。",
            "template": templates.text_for("REPRICE_MANUAL_HINT", self.conv.channel),
        }

    def quote_ticket_inquiry(self, evidence):
        if self.conv.channel != "wanda":
            return {"ok": False, "reason": "该工具仅用于万达询价；良票请查询当前报价或补齐选座信息。"}
        from application.ticket_flow import FlowResult
        count = inquiry_quantity(self.text)
        message = "请确认本次想询价的张数和场次，我再帮你核价。"
        valid = False
        current_show_only = bool(re.fullmatch(
            r"(?:请问|那|我要|要|买)?\s*(?:(?:第)?[0-9一二两三四五六七八九十]{1,3}排[，,、 ]*)?"
            r"[0-9一二两三四五六七八九十]{1,3}\s*张(?:票)?\s*(?:一共|总共|要)?"
            r"(?:多少钱|什么价|怎么卖|价格多少|几块|几元)[？?。!！ ]*", self.text.strip()))
        if (str(evidence).strip() == self.text.strip() and is_price_inquiry(self.text)
                and count and current_show_only and self.conv.channel == "wanda"):
            rows = self.service.quote_store.by_identity(self.identity) if all(self.identity.values()) and self.service.quote_store else []
            q = rows[0] if rows else None
            snap = self.session.image_context or {}
            fresh = False
            try:
                fresh = (all(snap.get(k) == v for k, v in self.identity.items())
                         and q is not None and q.created_at >= datetime.fromisoformat(snap["created_at"]))
            except (KeyError, TypeError, ValueError):
                pass
            if q and q.provider == "WANDA" and q.is_usable() and q.unit_sell_price_fen > 0 and fresh and q.metadata.get("price_mode") == "FIXED":
                total = q.unit_sell_price_fen * count
                message = (f"{q.cinema}｜{q.movie}｜{q.show}\n"
                           f"按当前场次报价 {q.unit_sell_price_fen / 100:.2f} 元/张，{count} 张合计 {total / 100:.2f} 元。")
                row = re.search(r"(?:第)?([0-9]{1,2}|[一二两三四五六七八九十]{1,3})排", self.text)
                if row:
                    message += f"你想要的是{row.group(0)}；具体位置及该区域是否同价需要核对，尚未确认座位。"
                message += "本次为询价，原订单未修改。"
                valid = True
            else:
                message = "当前场次还没有可用的单价，请补充或重发最新场次截图，我再核对这次的价格。"
        self.flow_result = FlowResult(ok=True, state=self.conv.state, channel=self.conv.channel,
                                     messages=[message], template_keys=["PRICE_INQUIRY"])
        return {"ok": valid, "handled": True, "guidance": message, "note": "询价回复已生成，结束本轮；不得改价。"}

    def supplement_city(self, city):
        if getattr(self, 'semantic_mode', False) and getattr(self, 'semantic_intent', None) not in ('revise', 'reprice'):
            return {'ok': False, 'reason': '补充城市前先确认买家是在修改当前购票需求。'}
        if self.city_attempted:
            return {"ok": False, "reason": "本轮已经处理过城市，不能重复识别"}
        if (
            not isinstance(city, str)
            or not re.fullmatch(r"[\u4e00-\u9fff]{2,16}", city)
            or city not in self.text
        ):
            return {"ok": False, "reason": "城市必须来自本轮买家原话，不能猜测"}
        from .city_confirmation import unchanged_quote
        confirmed = unchanged_quote(self, city)
        if confirmed:
            from .ticket_flow import FlowResult
            self.city_attempted = True
            message = f'收到，是{confirmed}，刚才的报价已保留。'
            self.flow_result = FlowResult(ok=True,state=self.conv.state,channel=self.conv.channel,
                messages=[message],template_keys=['CITY_CONFIRMED'])
            return {'ok':True,'status':'UNCHANGED','handled':True,'city':confirmed,
                    'quote_id':self.conv.quote_id,'reason':'城市与已核实报价一致；未重新识别、核价或修改订单。'}
        return self._resume_quote(city=city)

    def supplement_ticket_count(self, evidence):
        if self.conv.channel == "wanda" and is_price_inquiry(self.text):
            return self.quote_ticket_inquiry(evidence)
        count = explicit_ticket_count(evidence)
        # Quantity tool accepts the complete buyer message; this prevents a model
        # extracting "两张" from a negated sentence such as "不要两张".
        if count is None or str(evidence).strip() != self.text.strip():
            return {"ok": False, "reason": "请明确本次需要几张票，不能从否定或含糊的话中猜张数"}
        from .quote_confirmation import unchanged_details
        unchanged = unchanged_details(self,ticket_count=count)
        if unchanged is not None:
            return unchanged
        return self._resume_quote(ticket_count=count)

    def _resume_quote(self, *, city=None, ticket_count=None):
        if self.session.image_context.get('ticket_details'):
            return {'ok':False,'reason':'新的影片、日期、场次或座位尚未核对，请通过 supplement_ticket_details 继续补全，不能沿用旧截图报价。'}
        if self.city_attempted:
            return {"ok": False, "reason": "本轮已继续报价，不能重复调用"}
        if not self.cfg.recognize_quote.auto_recognize:
            return {"ok": False, "reason": "自动识别报价已关闭，请转人工"}
        rows = self._orders()
        from .purchase_continuation import separate
        if separate(self):
            from .assistant_reply import _is_current_order
            rows = [o for o in rows if _is_current_order(self, {'demand_id': o.demand_id, 'demand_rev': o.demand_rev})]
        if any(
            o.payment_state != "UNPAID" and o.state not in CLOSED_STATES for o in rows
        ):
            return {
                "ok": False,
                "status": "PAID_ORDER_LOCKED",
                "reason": "已有付款或处理中的订单，补充信息需人工核对，不自动替换",
            }
        if any((o.payload or {}).get("quote_revision_pending") for o in rows):
            return {"ok": False, "reason": "有改价结果待确认，请先核对原订单"}
        snap = self.image_snapshot
        if snap.get("unverified_image"):
            return {"ok": False, "reason": "补充图片尚未核对，请先确认是否仍按上一场购票"}
        if snap != (self.session.image_context or {}):
            return {
                "ok": False,
                "reason": "截图已经更新，本轮补充不能应用到另一张截图，请按最新截图重新确认。",
            }
        if any(snap.get(k) != v for k, v in self.identity.items()):
            return {"ok": False, "reason": "没有属于当前会话的截图，请重新发送截图"}
        try:
            age = (
                datetime.now(timezone.utc) - datetime.fromisoformat(snap["created_at"])
            ).total_seconds()
        except (KeyError, ValueError, TypeError):
            age = -1
        images = getattr(self, 'official_dialogue', {}).get('images', [])
        anchored_image = any(r.get('url') == snap.get('url') and r.get('speaker') == 'buyer'
                             and all(r.get(k) == v for k, v in self.identity.items()) for r in images)
        memory_limit = max(1, min(24, self.cfg.assistant.memory_hours)) * 3600
        if age < 0 or (self.conv.channel != "wanda" and age > 1200 and not (anchored_image and age <= memory_limit)) or not snap.get("url"):
            return {"ok": False, "reason": "上一张截图已过期，请重新发送截图"}
        if self.conv.state not in {
            ConvState.IDLE,
            ConvState.QUOTED,
            ConvState.REWORK,
            ConvState.QT_FAIL,
            ConvState.PICK_CINEMA,
            ConvState.PICK_MOVIE,
            ConvState.PICK_SHOW,
            ConvState.AWAIT_PAY,
            ConvState.AWAIT_REPRICE,
        }:
            return {"ok": False, "reason": "当前进度不能重新匹配，请转人工核对"}
        params = {"city_name": city or snap.get("city"), "channel": snap.get("channel")}
        count = ticket_count if ticket_count is not None else snap.get("ticket_count")
        if count is not None:
            params["ticket_count"] = count
        from .image_history import restore
        restore(self)
        candidate = self.session.image_context.get('cinema_candidate') or {}
        if candidate:
            from .ticket_flow import FlowResult
            if count is not None:
                self.session.image_context['ticket_count'] = count
            count_note = f"已保留{count}张需求。" if count is not None else ''
            message = count_note + '前面影院图显示的是：' + candidate['cinema'] + '。请确认本次仍是这家影院吗？确认后我继续核对最新场次和标记位置，不用重发全部信息。'
            self.flow_result = FlowResult(ok=True, state=self.conv.state, channel=self.conv.channel,
                messages=[message], template_keys=['TICKET_DETAILS'])
            return {'ok': True, 'status': 'NEEDS_INFORMATION', 'reason': message,
                    'source_message_id': candidate['source_message_id']}
        from copy import deepcopy
        previous = deepcopy(self.session.image_context)
        self.city_attempted = True
        self.flow_result = self.service.flow.on_screenshot(self.conv, snap["url"], **params)
        from .image_sequence import recover_sequence
        self.flow_result = recover_sequence(self.service, self.session, previous, snap['url'], self.flow_result, self.cfg)
        from .image_context import preserve_image_result
        preserve_image_result(self.session, {}, {}, snap["url"], self.flow_result)
        if city:
            self.session.image_context["city"] = city
        if ticket_count is not None:
            self.session.image_context["ticket_count"] = ticket_count
        return {
            "ok": bool(self.flow_result.ok),
            "handled": True,
            "note": "系统已经产生本次识别或报价回复，直接结束，不要再编写报价。",
        }
