# 新插件 UI-only 开发交接书

> 目标：只复制“万达电影票 AI 客服”工作台的视觉和交互，不复制原 V4 的回复、识图、报价、账号池、万达接口或交易架构。

## 一、边界

新插件只实现：

- 对话页面视觉布局；
- 买家文本输入；
- 图片选择、粘贴、拖拽和本地预览；
- 发送按钮、Enter 发送、Shift+Enter 换行；
- AI 回复气泡；
- 可选的结构化结果卡片；
- 可选的附件/图片展示；
- 加载中状态和耗时显示；
- 错误提示；
- 清空当前会话；
- 响应式布局；
- 插件自己的回复接口适配。

明确不实现：

- 万达账号池；
- 万达认证 Header 和签名；
- 万达场次/座位/报价接口；
- W+ 临时探针；
- 订单创建、改价、取消、出票；
- AI 回复决策；
- V4 Event Inbox、Command Outbox；
- 任何生产配置保存。

## 二、视觉参考

参考文件：

```text
E:/鱼麦多/v4/static/index.html
```

但新插件建议拆分为：

```text
ui/
  index.html
  app.js
  styles.css
  api-adapter.js
```

禁止直接复制 V4 的后端业务代码。可以复用颜色、布局、卡片和交互结构。

## 三、页面布局

```text
插件页面
  ├─ 顶部 Agent Header
  │   ├─ 红色 AI 头像
  │   ├─ 插件名称
  │   ├─ 在线状态
  │   ├─ 当前模式标签
  │   └─ 清空会话按钮
  ├─ 中间聊天窗口
  │   ├─ 日期分隔线
  │   ├─ AI 消息
  │   ├─ 买家消息
  │   ├─ 图片消息
  │   ├─ 结构化结果卡片
  │   └─ Loading 气泡
  └─ 底部 Composer
      ├─ 添加图片按钮
      ├─ 待发送图片预览
      ├─ 文本输入框
      └─ 发送按钮
```

如果新插件不需要顶部设置、日志或报价页面，不要复制这些工作区。

## 四、推荐前端状态

```js
const state = {
  conversationId: crypto.randomUUID(),
  file: null,
  pendingObjectUrl: null,
  sending: false,
  messages: [],
};
```

要求：

- `conversationId` 只用于当前 UI 会话；
- 清空会话时生成新的 ID；
- 图片预览使用 `URL.createObjectURL()`；
- 删除或发送后调用 `URL.revokeObjectURL()`；
- 不把 Token、Cookie、账号密码放进 state；
- 不把完整平台订单对象放到 DOM 或 localStorage。

## 五、回复架构适配层

由于新插件的回复架构不同，UI 不得假设 V4 的固定响应结构。

UI 只依赖一个适配器：

```js
export async function sendToPlugin({ conversationId, text, file }) {
  // 由新插件开发者实现
  // 这里可以调用插件自己的后端、SDK 网关或 WebSocket
  return {
    status: 'success',
    message: {
      text: '回复文本',
      attachments: [],
      cards: [],
    },
  };
}
```

统一返回格式建议：

```ts
interface UiReplyResult {
  status: 'success' | 'warning' | 'error';
  message?: {
    text?: string;
    attachments?: Array<{
      type: 'image' | 'file';
      url?: string;
      name?: string;
    }>;
    cards?: Array<{
      title: string;
      fields: Array<{ label: string; value: string }>;
      note?: string;
    }>;
  };
  error?: {
    code: string;
    message: string;
  };
}
```

如果新插件回复接口完全不同，只修改：

```text
ui/api-adapter.js
```

不要修改聊天渲染组件。

## 六、发送接口不要写死

不要在通用 UI 里写死：

```js
fetch('/api/chat/image-messages')
fetch('/api/chat/text-messages')
```

改成：

```js
const result = await sendToPlugin({
  conversationId: state.conversationId,
  text,
  file,
});
```

新插件可以在适配层实现：

### 文本请求

```http
POST /plugin-local/reply
Content-Type: application/json
```

```json
{
  "conversation_id": "...",
  "text": "买家文字"
}
```

### 图片请求

```http
POST /plugin-local/reply
Content-Type: multipart/form-data
```

```text
conversation_id
text
image
```

如果文本和图片在新架构中走不同接口，也只在适配层分流。

## 七、核心交互

### 选择图片

校验：

```text
允许：image/jpeg、image/png、image/webp
最大大小：由新插件配置决定，默认不超过10MB
```

不符合时只显示 UI 错误，不发请求。

### 发送消息

流程：

```text
读取输入
  ↓
本地追加买家消息
  ↓
清空输入框和待发送文件
  ↓
显示 loading 气泡
  ↓
调用 sendToPlugin()
  ↓
删除 loading
  ↓
按统一结果渲染 AI 文本、附件和卡片
  ↓
恢复发送按钮
```

### 错误

适配器失败时返回：

```json
{
  "status": "error",
  "error": {
    "code": "reply_unavailable",
    "message": "暂时无法处理，请稍后重试。"
  }
}
```

UI 不显示堆栈、Token、内部 URL 或数据库错误。

## 八、组件建议

至少拆成以下函数或模块：

```text
createMessageBubble()
createTypingBubble()
createAttachmentPreview()
createFactCard()
appendMessage()
selectFile()
clearFile()
sendMessage()
resetConversation()
renderReply()
```

渲染用户或后端文本时使用：

```js
node.textContent = value;
```

不要使用：

```js
node.innerHTML = value;
```

除非经过严格 HTML 白名单清洗。

## 九、样式建议

可复用以下视觉风格：

```css
:root {
  --brand: #f04444;
  --brand-dark: #d92e35;
  --ink: #232326;
  --muted: #85858d;
  --line: #e9e9ed;
  --canvas: #f5f5f7;
}
```

建议：

- 页面背景使用浅青色渐变；
- 主聊天框白色、圆角 16px；
- AI 消息左侧白色气泡；
- 买家消息右侧红色渐变气泡；
- 聊天区域使用浅灰点阵背景；
- 结果卡片使用浅灰边框；
- 小屏幕下隐藏不必要的 Header 文本；
- 使用 CSS Grid 实现 Header / Chat / Composer 三行布局。

## 十、必须满足的安全要求

- UI 不保存任何后端密钥；
- 不在 localStorage 保存完整聊天敏感信息；
- 图片预览 URL 用完释放；
- 发送接口使用同源路径或安全网关；
- 不能由用户输入拼接 HTML；
- 错误提示不能泄漏内部接口地址；
- 图片大小和 MIME 类型双重校验；
- 发送按钮在请求期间禁用；
- 防止重复点击产生重复请求；
- 回复架构产生的内部字段不直接显示，除非适配器明确映射为安全字段。

## 十一、验收标准

### 视觉

- 页面与参考截图具有相同的整体层级、颜色、圆角和气泡布局；
- 桌面端和移动端可用；
- AI 和买家消息方向清晰；
- 图片预览尺寸受控；
- loading 状态不会导致页面跳动。

### 交互

- 点击上传可选图；
- 支持粘贴图片；
- 支持拖拽图片；
- Enter 发送；
- Shift+Enter 换行；
- 空消息不可发送；
- 发送中不可重复提交；
- 清空会话后生成新 conversation ID；
- 后端失败能显示安全错误。

### 架构

- 修改回复接口时只需修改 `api-adapter.js`；
- UI 不依赖万达报价字段；
- UI 不依赖 V4 状态机；
- UI 不接触账号池和官方接口；
- UI 不含生产密钥；
- 可用 mock adapter 独立运行和测试。

## 十二、给开发 AI 的指令

只开发新插件 UI，不改动回复业务架构。先读取新插件现有目录、入口和 UI 挂载方式，然后创建独立的 `ui/index.html`、`ui/app.js`、`ui/styles.css` 和 `ui/api-adapter.js`。视觉参考 `E:/鱼麦多/v4/static/index.html`，但只迁移界面和交互，不迁移任何识图、报价、万达接口、账号池、订单和规则代码。回复接口必须通过适配器隔离。先使用 mock adapter 完成视觉和交互测试，再接入新插件自己的回复接口。不得部署生产，不得修改生产配置，不得操作真实订单。
