# 万达电影票 AI 客服 V3

面向鱼麦多的万达电影票客服系统。客服对话由 Agent 编排，价格、实时座位、订单状态和改价权限均由受控的确定性服务验证。

## 主要组件

- `plugin-auto-reply-work/`：鱼麦多插件；接收平台事件、维护会话上下文、调用受控工具并发送回复。
- `v3-backend-gateway-work/`：万达业务网关；处理截图识别、场次匹配、实时座位核价、报价与订单门禁。
- `deploy/`：部署与运维脚本。
- `BUSINESS_RULES.md`：业务规则与安全边界。

## 核心边界

- 价格唯一来源是万达实时座位图和后台报价规则；模型、截图和经验不得猜价。
- 手绘圈选仅表示人工出票偏好，不能伪造座位或承诺余票。
- 临时锁座仅用于受控核价，必须安全释放；释放未确认时不返回价格。
- 出票、退款、发货始终人工处理。
- Agent 不能直接提交金额、订单号、座位 ID、账号或密钥给高风险动作。

## 本地开发

插件：

```powershell
Set-Location .\plugin-auto-reply-work
npm install
npm test
```

业务网关：

```powershell
Set-Location .\v3-backend-gateway-work
python -m pip install -r requirements.txt
python -m pytest
```

## 配置与数据

运行时配置、密钥、Cookie、买家数据、日志、依赖目录和构建产物都被 `.gitignore` 排除。仅提交示例配置，真实凭据应通过受限环境变量或部署平台配置。

启用万达官方直连的发布必须先执行无敏感信息的环境预检：

```bash
cd v3-backend-gateway-work
python scripts/validate_direct_gateway_env.py --require-enabled
```

预检验证直连开关、独立 `WANDA_PRICING_ACCOUNT_REF_KEY`、账号池文件权限和可用W+账号数量；输出不包含密钥、Token、手机号、账号标识或账号池路径。
