# WorkTwin Collector 1.0.1 · macOS 安装修复

- 修复 macOS App 签名后的资源变更：Rowboat 许可证和声明现在在 PyInstaller 打包签名前纳入应用，不再复制到已签名的 .app 内。
- PyInstaller 严格验证 App 代码签名与资源封装；DMG 构建后校验容器完整性。
- GitHub macOS CI 不再只启动打包目录里的可执行文件，而是挂载最终 DMG、校验签名、复制到独立应用目录并进行真实 SQLite / HTTP 启停测试。
- macOS Gatekeeper 评估被明确记录：**ad-hoc 签名不能代替 Developer ID 与 Apple 公证**，公开测试安装包标注 `-unsigned`。
- 新增 [macOS 安装/校验和/问题排查](docs/MACOS_INSTALL.md)，说明如何在验证来源后进行仅针对 WorkTwin 的单应用安全例外。
- 1.0.1 为打包与安装可靠性修复版，业务功能不变，数据结构不变；禁止在公开仓库提交用户数据或签名凭据。

## 仍需补充的发布条件

普通员工浏览器下载后免例外安装，必须拥有 Apple Developer ID Application 签名证书和 Apple `notarytool` 公证凭据。当前公共 CI 尚未配置，**不能宣称本版已被 Apple 信任**。没有在用户本人 macOS 上完成独立的首次安装验收。

---

# WorkTwin Collector 1.0.0

本地采集 → 可核对、可维护的知识 → 按知识授权的数字分身 → 可到期、可撤销的远程访问，形成可运行的完整试点闭环。

- 保留三个入口。采集、AI 整理、分身分享独立授权；企业统一 BYOK，员工无供应商 Key，无 Ollama。
- 知识支持安全 Markdown、稳定 ID 链接、反向链接、有效共同来源、历史版本和 Markdown Wiki 导出；保留跨文档更新审核及空闲期知识养护。
- 实际移植 Rowboat Markdown 字段/列表/标题解析的一小部分，附 Apache-2.0 原始许可和改动说明。其他竞品仅为流程设计参考。
- 分享发布已确认且获准的知识快照。本人电脑离线后，接收者仍可查询；每条持有者访问链接有期限、可撤销。多分身复用同一知识资产。
- 服务端固定模型路由，持久日调用/Token 限额、分钟限流、并发控制、内容不进入审计；回答验证引用，运行中撤权/快照更新阻止旧答案返回。
- 任务使用领取凭据和过期恢复，避免扫描或打开新数据库对象重置仍在运行的任务。
- 发布 macOS arm64/x86_64 DMG、Windows x64 ZIP、源码 ZIP、Python wheel/sdist 和 SHA256 校验文件。

## 使用条件和已知限制

管理员需自托管企业服务并配置真实模型凭据、HTTPS 与员工令牌。访问链接是持有者凭据，接收者标签不等于企业身份认证；尚未集成 SSO。客户端离线修改权限时，远程快照在恢复同步前仍可访问，界面会显示待同步状态。

桌面包未签名、未公证。自动测试使用合成资料和确定性测试模型，不代表真实模型在员工历史资料上的知识准确率。尚未验收组织 IdP、生产 DLP、系统长期休眠/唤醒和自动更新。调用 Token 限额不是货币账单上限。

[部署与运行](https://github.com/KirenHu/AI-coding/blob/main/worktwin-collector/docs/DEPLOY.md) · [验收范围](https://github.com/KirenHu/AI-coding/blob/main/worktwin-collector/docs/ACCEPTANCE.md)
