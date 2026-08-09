# PCLModPage — PCL2 模组推荐主页生成器

为 [PCL2 启动器](https://github.com/Hex-Dragon/PCL2) 生成类 Steam 风格的 **Minecraft 模组推荐主页**，数据源为 [Modrinth API](https://docs.modrinth.com)。

## 功能

- 🎮 每天推荐 **12 个好玩的内容模组**（冒险/魔法/生物/科技/世界生成等 14 个分类），自动排除 library/优化/工具类基础模组
- 🆕 默认只推**最近 90 天发布的新模组**（`NEW_DAYS` 可调），打分兼顾"新度 + 关注/下载"
- 📅 以当天日期为种子加权抽样，**每天换一批、近 14 天不重复**（`history.json` 去重）
- 🇨🇳 简介经 [MCIM 镜像](https://mod.mcimirror.top) 自动换成**中文**，失败回退英文
- 🖼️ 图片直接用 Modrinth CDN 的 webp（PCL2 的 MyImage 原生支持）
- ⬇️ 每张卡带「下载」按钮，点击**直接下载到当前版本 mods 文件夹**（PCL2 `下载文件` 事件）
- 🌐 附 GitHub Action 部署模板（可选，需配置服务器 Secrets）

## 使用

```bash
cd ModPage
pip install requests pillow   # Pillow 仅 --local-images 模式需要
python main.py                # 生成 ModPage.xaml（默认 12 个模组, 2 列）
python main.py --limit 16     # 改数量
python main.py --dry-run      # 只预览不写历史
python main.py --local-images # 图片下载到本地转 png(自托管用; 默认直接引 CDN)
```

生成 `ModPage.xaml` 后，在 PCL2 → 设置 → 下载 → 自定义主页 中订阅该文件（本地路径或服务器 URL）。

## 配置

编辑 `modrinth_api.py` 顶部：

| 配置 | 说明 |
|---|---|
| `NEW_DAYS` | 只看最近 N 天发布的新模组（调大如 9999 可包含老牌） |
| `MIN_FOLLOWS` | 质量底线（关注数下限） |
| `W_FOLLOWS / W_DOWNLOADS / W_RECENT` | 打分权重：关注 / 下载 / 新度 |
| `GAME_VERSION` / `LOADER` | 按 MC 版本 / 加载器过滤 |
| `USE_TRANSLATE` | 是否用 MCIM 中文翻译简介 |

## 验证

```bash
python test/rotate_check.py   # 模拟连续 20 天，断言与近 14 天推送零撞车
```

## 协议

本主页使用 CC BY-NC-SA 4.0 协议。
