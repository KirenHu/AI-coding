# macOS 安装与启动排查

先核对下载版本，再选择适合自己芯片的安装包。Apple Silicon（M 系列）使用 arm64，Intel 使用 x86_64。

## 下载和安装

1. 前往 [GitHub Releases](https://github.com/KirenHu/AI-coding/releases)，查看目标版本的发布说明及附件。若未提供当前源码版本的安装包，可[从源码运行](GETTING_STARTED.md#从源码运行)。
2. 如果发布页提供 `SHA256SUMS.txt`，用 `shasum -a 256 <下载的DMG路径>` 核对文件哈希。
3. 打开 DMG，把 `WorkTwin.app` 拖入「应用程序」，再从应用程序目录启动。
4. 如果页面没有自动出现，访问 <http://127.0.0.1:8765>。

截至 2026-10-14，最新正式 Release 是 1.1.7。1.5.9 已有 Developer ID 签名、Apple 公证与本机安装验收记录，但尚未正式发布到 Releases；不要把研发验收产物与正式发布混为一谈。详细状态见 [1.5.9 交付记录](P0_P1_DELIVERY_1_5_9.md)。

## 提示无法验证开发者或应用已损坏

文件名带 `-unsigned` 的预览包没有正式 Developer ID 签名与公证。签名封装完整也不等于 Gatekeeper 信任。

先核对来源、下载版本和哈希；可使用下面的命令检查应用签名是否完整：

```bash
codesign --verify --deep --strict --verbose=2 /Applications/WorkTwin.app
```

若校验失败，重新下载或反馈错误。校验通过但仍被拦截时，优先使用已签名、公证的对应版本，或选择源码运行。需要明确允许运行某个可信测试包时，使用 macOS「系统设置 → 隐私与安全性」提供的单应用打开选项；不要关闭全局 Gatekeeper。

反馈问题时附上 macOS 版本、芯片、DMG 文件名、错误提示和本地页面是否可访问即可，不需要上传个人知识库或密钥。

## 升级与回退

升级前在 WorkTwin 设置中备份数据库，通过应用「退出」停止后台，再替换应用。关闭浏览器页面不会退出后台。

回退应同时恢复与旧版本兼容的数据库备份。仅替换旧 App、继续使用已迁移的新数据库，不能视为可靠回退。

## 面向发布维护者

正式交付需完成 Developer ID 签名、Apple 公证、票据附加以及 Gatekeeper 安装验证。CI 启动成功不能代替下载后的首次打开验证。流程见 [macOS 签名配置](MACOS_SIGNING.md)。

[返回入门指南](GETTING_STARTED.md) · [文档导航](README.md)
