# 部署说明 · v0.4

## 1. 员工设备：采集客户端

推荐 Python 3.11+，本地安装见 [README](../README.md)。无需 Ollama，也不要求终端用户持有付费模型的供应商 API Key。

启动前由组织设备管理配置：

```bash
export WORKTWIN_GATEWAY_URL="https://worktwin-gateway.company.example"
export WORKTWIN_GATEWAY_TOKEN="<employee-access-token>"
python -m worktwin serve --open
```

HTTP 网关 URL 只允许 `localhost/127.0.0.1` 单机开发使用，远程统一网关需要 HTTPS。该 token 在试点采用简单 Bearer 策略，生产应替换为企业 IAM 发放和周期轮换的短期凭据。

## 2. 企业服务器：BYOK 网关

在服务器配置以下**服务端专属**变量，不写入员工安装包或仓库：

```bash
export WORKTWIN_BYOK_BASE_URL="https://your-compatible-provider.example/v1"
export WORKTWIN_BYOK_MODEL="your-organization-approved-model"
export WORKTWIN_BYOK_API_KEY="<your-organization-secret>"
export WORKTWIN_ENTERPRISE_TOKENS="<employee1-token>,<employee2-token>"
python -m uvicorn worktwin.gateway:create_gateway --factory --host 127.0.0.1 --port 8789
```

这里的 `127.0.0.1` 期望由企业 TLS 反向代理及身份服务在前方完成认证和路由。**切勿直接将当前试点网关绑到 `0.0.0.0` 并暴露公网**。网关固定企业模型，员工提交的消息即使包含 `model` 也不会覆盖付款配置。

## 3. 运行保障与限制

- 按来源单独启用「允许 AI 整理」。默认禁止把本地资料发送给企业模型。
- 模型调用失败会标记任务为 `error`；HTTP API 支持管理员调试时重新入队，定时 Worker 每轮只处理一个文档以限制突发调用。上线需增加租户、员工和项目级额度、超时、退避重试、计费与审计。
- 本地 SQLite 包含解析后的文档文字，应加密硬盘并为备份设置保留期。数据库由个人设备存储；目前没有组织级云同步。
- 多人访问权限、离职移交、共享给其他员工的数字分身 URL、移动端、企业 IdP/SSO、真实数据 DLP 和 CDN/网关生产 SLA 不属于 v0.4 交付。
- `desktop/build-macos.sh`、`desktop/build-windows.ps1` 可以在对应操作系统构建未签名封装；构建出的客户端需要相应平台验收和签名公证才能发给企业员工。
