# 鱼麦多插件本地目录

这是鱼麦多插件的整理后工作区。出票系统主仓库仍位于 `E:/票务系统`，不要把两个项目混在一起。

## 目录职责

- `backend/`：鱼麦多 V4 Python/FastAPI 业务后端，原 `app/` 和 `tests/` 已迁入。
- `frontend/v4/`：V4 独立管理页面，原 `static/` 已迁入。
- `plugin-runtime/wanda-seat-autoquote/`：鱼麦多平台运行时、插件页面、事件处理和订单执行代码。
- `data/`：本地配置、数据库和运行数据，禁止上传 GitHub。
- `logs/`：本地日志，禁止上传 GitHub。
- `deploy/`：本地部署脚本和发布记录。
- `*.migration-backup-*.tar.gz`：迁移前源代码备份，不包含 `data/`、`logs/` 或缓存。

## 启动

从本目录运行 `start.ps1`。脚本会把 `backend/` 加入 Python 模块路径，并继续使用根目录的 `data/` 和 `logs/`。

## 服务器对应关系

| 本地组件 | 服务器运行目录 | 当前发布目录 | 服务 | 端口 |
|---|---|---|---|---|
| 独立出票系统后端 | `/opt/ticket-system/current/backend` | `ticket-batch-coupon-bind-20260829-170504` | `ticket-backend.service` | `127.0.0.1:8000` |
| 鱼麦多 V4 后端 | `/opt/wanda-v4/current` | `v4-2.4.93-knowledge-policy-reminder-20260829-195857` | `wanda-v4.service` | `127.0.0.1:8012` |
| 鱼麦多插件运行时 | `/opt/wanda-seat-autoquote/current` | `plugin-2.2.50-conversation-knowledge-20260829-201718` | `wanda-seat-autoquote.service` | `127.0.0.1:4003` |

V4 后端和插件运行时共同组成鱼麦多插件；出票系统是另一个系统。`current` 是软链接，旧版本保留在各自的 `releases/` 目录用于回滚。

## 修改和同步规则

以后优先修改本目录对应的组件。修改前先检查 GitHub 和其他 AI 的最新提交；测试通过后再提交、推送和部署。当前旧目录 `E:/票务系统/plugins/wanda-seat-autoquote` 保留为迁移期间的镜像，未完成 Git 同步前不要在两处同时修改。
