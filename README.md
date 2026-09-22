# 糖宝桌面宠物 · desktop-pet for herdr

一只常驻桌面的像素宠物（糖宝皮肤），轮询 herdr 的 socket API，监视各 agent 任务的状态变化。
任务**完成**时庆祝（眨眼小拳头 + 提示音），**思考中**时流口水，**等确认**时瞪眼报警，
**开始干活**时轻提示。点击宠物跳转到最近通知的任务，拖拽记住位置。

## 从市场安装

本插件发布在 [herdr.dev/plugins](https://herdr.dev/plugins/)（通过 `herdr-plugin` topic 自动收录）：

```powershell
herdr plugin install ikundasdas/herdr-
herdr plugin list          # 确认 desktop-pet 已出现
herdr plugin log desktop-pet  # 查看钩子运行日志
```

之后任意 agent 状态变化都会拉起宠物。

## 环境要求

- Windows，`herdr >= 0.9.0`
- Python 3（带 tkinter），且 `pythonw` 在 PATH 里（钩子用 GUI 子系统启动，无黑窗闪烁）

## 组成

| 文件 | 作用 |
| --- | --- |
| `herdr-plugin.toml` | 插件清单：`[[startup]]` + `pane.created` / `pane.agent_status_changed` 钩子，全部跑 `ensure_pet.py` |
| `ensure_pet.py` | 一次性引导：写一行事件到 inbox、确保宠物进程唯一在跑（命名互斥体），没跑就拉起 `pythonw main.py` |
| `herdr_client.py` | herdr socket 客户端（命名管道 + 会话快照兜底） |
| `main.py` | 宠物大脑：轮询、状态机、气泡/提示音调度、离线策略 |
| `pet_render.py` | tkinter 渲染器：72 帧预载、状态徽章、拖拽、右键菜单 |
| `pet_render_headless.py` | 控制台版渲染器（契约一致，用于无 GUI 自测） |
| `dsh_watcher.py` / `dsh_ctl.py` | DSH 会话监视 + DSH Web 一键启停 |
| `assets/frames/` + `assets/frames_meta.json` | 皮肤：四档分辨率（native/m96/m72/m54）各 72 帧 + 帧契约 |

美术与第三方素材说明见 `assets/LICENSES.md`。

## 配置

`%LOCALAPPDATA%\herdr-desktop-pet\config.json`（不存在则用默认值；拖拽结束自动写回 position。
若钩子环境提供 `HERDR_PLUGIN_CONFIG_DIR` / `HERDR_PLUGIN_STATE_DIR` 则优先使用它们）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `poll_ms` | `1500` | 轮询间隔（毫秒） |
| `notify` | `["done","blocked","working"]` | 哪些状态迁移值得提示 |
| `sound` | `true` | done/blocked 是否发声 |
| `scale` | `"m72"` | 分辨率档：`native`(192x208) / `m96` / `m72` / `m54` |
| `position` | `null` | 宠物窗口位置，拖拽后自动保存 |
| `language` | `"zh"` | 界面文案语言 |

## 换肤

任一皮肤 = 同一 72 帧契约（行/时序、二进制 alpha）替换 `assets/frames/` +
`assets/frames_meta.json`。运行时只读这两处；状态映射见 `pet_render.py` 的 `STATE_ANIM`。

## 卸载

```powershell
herdr plugin uninstall desktop-pet
```

再手动结束 `pythonw.exe`（宠物进程），并按需删除
`%LOCALAPPDATA%\herdr-desktop-pet\` 与 `%TEMP%\herdr-desktop-pet*.log`。
