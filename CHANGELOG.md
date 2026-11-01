# 更新记录

## 1.0.1 — 2026-11-02

### 新增

- 插件 WebUI 设置页「消息卡片设置」，支持可视化修改配置、保存后即时生效和实时预览。
- 卡片标题可通过 `header.title` 自定义；WebUI 对应「卡片标题」字段。
- 页眉/页脚图片 API 示例与 Docker/127.0.0.1 文档链接。
- README 与 WebUI 中的 `meme_manager` 可点击跳转到其 GitHub 仓库。

### 改进

- 默认卡片宽度 720px、正文字号 28px，手机端无需放大即可阅读。
- 正文图片与表情包统一按固定长边 380px 等比缩放，不裁切、不拉伸。
- 发送者区域显示 QQ 头像；页脚显示机器人昵称和机器人头像，获取失败回退首字。
- 保留 AstrBot 分段回复、TTS、转发字数阈值的兼容开关。

### 修复

- `card_renderer.py`、`card_assets.py` 统一使用 `from astrbot.api import logger`，移除内置 `logging.getLogger("astrbot")`。

## 1.0.0 — 2026-11-01

- 首个版本：Markdown / HTML / LaTeX / 图片卡片渲染，昼夜主题、页眉页脚图片 API、Docker 回退与安全清洗。
