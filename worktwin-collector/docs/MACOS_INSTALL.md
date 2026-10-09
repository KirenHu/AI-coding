# macOS 安装与“App 已损坏”排查（v1.1.2）

WorkTwin 在 GitHub Actions 上用 PyInstaller 构建。1.0.0 曾在打包后的
`WorkTwin.app` 内追加许可文件，可能使 PyInstaller 刚完成的资源签名失效。
1.0.1 把这些资料纳入 PyInstaller 数据文件，再要求严格校验最终 App 签名、
验证 DMG 完整性，并在 CI 里从 **真正的 DMG** 挂载、复制和运行。

**注意：v1.1.2 的公开测试安装包仍属于 ad-hoc 签名（文件名带 `-unsigned`），没有 Apple
Developer ID 签名和 Apple 公证。签名完整不等于 macOS Gatekeeper 信任。**
Chrome/Safari 下载后可能显示“无法验证开发者”或“已损坏，无法打开”等安全拦截。
macOS 无需永久关闭任何系统安全保护。

## 安装前核对

1. 只从 [WorkTwin 官方 GitHub Release](https://github.com/KirenHu/AI-coding/releases/tag/worktwin-v1.1.2) 下载对应架构。
   Apple Silicon（M 系列）选择 `macOS-arm64-unsigned.dmg`；Intel 选择 `macOS-x86_64-unsigned.dmg`。
2. 同页下载 `SHA256SUMS.txt`。打开终端执行（以 Apple Silicon 为例）：

   ```bash
   cd ~/Downloads
   shasum -a 256 WorkTwin-Collector-1.1.2-macOS-arm64-unsigned.dmg
   ```

   将输出与 `SHA256SUMS.txt` 中对应一行**逐字比较**。不一致时删除文件重新下载，切勿绕过安全检查。
3. 双击 DMG，并将 `WorkTwin.app` **复制到“应用程序 / Applications”目录**，不要直接从 DMG 长期运行。
4. 在终端核对应用签名封装未损坏：

   ```bash
   codesign --verify --deep --strict --verbose=2 /Applications/WorkTwin.app
   ```

   如果失败，先停止，不要取消隔离；反馈 `codesign` 的报错以及所下载的文件名。

## 试用版遇到 Gatekeeper 阻止

推荐首先尝试 Finder 中右击应用选择“打开”；系统提供“仍要打开”选项时，
在“系统设置 → 隐私与安全性”明确授权该**单独应用**。

若仍然显示“已损坏”，而且已经确认来自上述 GitHub 发布、SHA256 与
`codesign --verify` 均正确，且你明确愿意运行这个未经过 Apple 公证的测试程序，
可以仅对该应用取消隔离：

```bash
xattr -dr com.apple.quarantine /Applications/WorkTwin.app
open /Applications/WorkTwin.app
```

这属于**针对 WorkTwin 单个 App 的安全检查例外**，不能作为面向普通员工的正式安装流程。
不要使用 `spctl --master-disable` 或关闭全局 Gatekeeper，也不要对其他下载程序批量执行 `xattr`。

如果应用打开后但页面没有自动出现，可检查 `http://127.0.0.1:8765`。
问题未解决时请提供 macOS 版本、芯片、DMG 文件名、`codesign` 输出以及本地服务是否响应，
**不要**上传密钥、个人知识库、原始会话或企业访问令牌。

## 生产发布必须补齐

- 使用 **Developer ID Application** 证书进行正式代码签名，并开启 Hardened Runtime。
- 通过 Apple `notarytool` 公证，将票据 staple 到交付的 DMG/App，且在启用 Gatekeeper 的
  全新 macOS 用户环境中验证从下载、复制到首次双击的完整流程。
- 把证书和公证凭据放在 GitHub Actions 的受保护 Secrets/Keychain 中，绝不能提交到公开仓库。
- CI 的 `codesign --verify` 只验证签名完整；`spctl --assess` 在没有 Developer ID
  时会拒绝，无论 Python 测试和启动 smoke 是否成功。不得据此宣称生产可安装。

官方背景：[Apple Developer 分发](https://developer.apple.com/macos/distribution/) ·
[PyInstaller macOS 签名](https://pyinstaller.org/en/stable/feature-notes.html)
