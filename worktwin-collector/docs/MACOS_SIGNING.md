# macOS 正式签名与公证

正式流程以 Developer ID Application + Apple 公证为发布条件。旧版 `-unsigned.dmg` 不因此变成可信安装包；必须下载新的正式 DMG。

## GitHub Secrets

仓库 `KirenHu/AI-coding` 使用以下 Secrets，值由本地标准输入交给 `gh secret set`，禁止将值放入命令参数、日志、聊天或源码：

| Secret | 内容 |
| --- | --- |
| `MACOS_CERTIFICATE_P12` | 含证书与私钥的 p12 文件的 Base64 |
| `MACOS_CERTIFICATE_PASSWORD` | p12 导出密码 |
| `APPLE_NOTARY_KEY_P8` | App Store Connect Team API Key 的 .p8 原文 |
| `APPLE_NOTARY_KEY_ID` | API Key ID |
| `APPLE_NOTARY_ISSUER_ID` | Team API Key 的 Issuer ID |

Apple 登录、双重认证、协议接受与 API Key 创建由账户持有人完成。可在 App Store Connect 的 Users and Access / Integrations / App Store Connect API 创建 Developer 角色 Team Key。私钥下载后保存在仓库之外。

## 执行与发布条件

- `main` 的 push 和手动 `workflow_dispatch` 构建 ARM64 / Intel 正式包。任何必需 Secret 缺失、Developer ID 无效、公证非 Accepted、票据校验或 Gatekeeper 失败都会阻断正式产物。
- PR 使用 ad-hoc 预览包，不导入私钥，也不公证。预览文件与 artifact 都带 `unsigned` 标识。
- CI 将签名材料放到 Runner 临时目录，以随机密码建立临时 Keychain。只向后续构建传递签名 identity 和 .p8 路径；最后始终尝试恢复 Keychain 列表并删除签名材料。
- PyInstaller 为应用及嵌套代码签名，并开启 hardened runtime。先提交 App ZIP，等待 Apple Accepted，staple App；然后封装、签名、公证并 staple DMG。
- 验收实际交付 DMG、App 签名、Developer ID Authority、runtime 与安全时间戳、App/DMG 票据、DMG Gatekeeper、复制后的 App 下载隔离标记及 Gatekeeper，以及冻结程序的 HTTP/SQLite 启停。
- 手动运行仅产生验收 artifact，不自动发布 GitHub Release。`main` 的发布任务只在所有平台验收通过后发布，并拒绝 unsigned macOS DMG。

## 首次分发验收

CI 通过后仍需账户持有人在正常启用 Gatekeeper 的 Mac 上，用浏览器下载正式 DMG，打开磁盘映像，将 WorkTwin.app 拖到 Applications，然后首次通过 Finder 打开。保留下载隔离标记；不通过取消隔离或关闭 Gatekeeper 代替验收。记录架构、macOS 版本、下载文件名及启动结果。

在 ARM64 和 Intel CI 均通过前，不宣称正式包已完成公证与验收。历史 1.0.1 Release 的 unsigned 文件说明仍适用于旧包。

参考：[Apple 公证流程](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)、[PyInstaller macOS 签名](https://pyinstaller.org/en/stable/feature-notes.html#macos-binary-code-signing)。

ARM64 Runner 使用 `macos-15`，Intel 使用 `macos-15-intel`。GitHub 已公告 macOS 14 将于 2026-11-02 退役：[Runner 退役公告](https://github.blog/changelog/2026-10-01-github-actions-macos-14-runner-image-retirement/)。
