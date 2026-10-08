# WorkTwin 1.0 部署

客户端与企业服务器是两个独立进程。企业服务器持有供应商 Key、保存明确授权的共享知识和访问授权；本机保存原始解析文本和个人知识。

## Docker Compose

```bash
cp enterprise.env.example enterprise.env
# 编辑 enterprise.env：供应商地址、模型、Key、独立员工令牌
# 生成令牌：python -c "import secrets; print(secrets.token_urlsafe(32))"
docker compose up -d --build
```

端口只绑定服务器的 `127.0.0.1:8789`。由组织 HTTPS 反向代理转发到它，外部地址例如 `https://worktwin.company.example`。代理超时应至少 150 秒，允许最长 4 MiB 发布请求，避免记录 Authorization 与分享 URL。`worktwin-data` 卷持久保存 `sharing.sqlite` 和 `usage.sqlite`；不要删除卷作为常规升级方式。

`WORKTWIN_PUBLISHER_TOKENS` 是 JSON 员工标识到独立随机令牌的映射，例如 `{"employee-a":"<random-token-a>","employee-b":"<random-token-b>"}`。真实值只留在受控配置中。新增/轮换令牌后重启服务；改变员工标识会改变原共享快照的所有者，应先处理旧授权。

## Python 启动

```bash
python -m pip install .
export WORKTWIN_BYOK_BASE_URL="https://your-compatible-provider.example/v1"
export WORKTWIN_BYOK_MODEL="your-enterprise-model"
export WORKTWIN_BYOK_API_KEY="<enterprise-key>"
export WORKTWIN_PUBLISHER_TOKENS='{"employee-a":"<random-employee-token>"}'
export WORKTWIN_SERVER_DATA_DIR="/srv/worktwin/data"
python -m uvicorn worktwin.server:create_server --factory --host 127.0.0.1 --port 8789
```

模型路由要求 HTTPS，本机模拟服务允许 HTTP localhost。`worktwin.gateway:create_gateway` 仍提供旧版仅模型网关兼容模式；完整分享需要 `worktwin.server:create_server`。

## 员工客户端

启动客户端，在侧栏「连接企业知识服务」输入外部 HTTPS 地址与个人令牌。也可由设备管理注入：

```bash
export WORKTWIN_SERVER_URL="https://worktwin.company.example"
export WORKTWIN_SERVER_TOKEN="<employee-token>"
python -m worktwin serve --open
```

只对单机开发允许 HTTP localhost。模型供应商 Key 不进入安装包。个人令牌保存在员工本机配置数据库；1.0 尚未使用 OS Keychain。已启用发布时禁止直接切换企业地址，先停止并确认撤销同步成功。

## 限额和持久状态

| 变量 | 默认 | 含义 |
|---|---:|---|
| WORKTWIN_DAILY_CALL_LIMIT | 1000 | UTC 日全服务调用上限 |
| WORKTWIN_DAILY_TOKEN_LIMIT | 2000000 | UTC 日 Token 总额度，未知用量保留保守估算 |
| WORKTWIN_MINUTE_CALL_LIMIT | 20 | 每个员工凭据/分享授权每分钟调用数 |
| WORKTWIN_MODEL_CONCURRENCY | 4 | 单服务进程同时模型请求上限 |

SQLite 事务先预留额度；供应商报告用量后更新。失败请求仍消耗调用次数与估算额度。审计记录凭据哈希、时间、模型、状态与用量，不记录提示正文/凭据明文。单实例运行是 1.0 推荐方式；不配置多个进程来绕过进程内并发上限。

## 撤销、备份与运行边界

客户端每 5 秒检查共享变化，成功本地修改也触发同步；企业不可达时显示待同步，旧发布内容不会因本地断网自行消失。紧急撤销可直接调用服务端 `DELETE /v1/twins/{remote_id}/grants/{grant_id}`，带该所有者的 Bearer Token。运行中问答完成后再次校验访问权限和快照版本。

停止本地采集或退出应用不会撤销已有分享。停止分享同步成功后，服务器删除相应分身及链接；快照中的未使用知识也被替换删除。SQLite 安全删除不替代存储设备与历史备份的删除策略。备份前停止写入或使用 SQLite backup API；恢复旧客户端备份可能导致发布版本过期，需管理员处理，不能以恢复备份作为撤权方案。

1.0 使用独立员工令牌和可撤销持有者链接，没有 SSO、组织资料原有 ACL 联动、企业 DLP 或自动证书/签名管理。进入组织生产需结合现有身份与运维系统完成这些部署工作，并使用实际业务资料验证知识质量。
