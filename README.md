# MaoMao Personal AI Assistant

猫猫是一个更像人的 Windows AI 助手：通用模型负责理解和思考，小技能负责执行具体动作，互动感是第一追求。

当前仓库处于 `beta` 阶段，默认采用 API 文本、转写和语音生成；记忆、权限、操作记录和个人配置保存在用户自己的电脑上。

当前版本：`v0.0.2_beta1`

## 特点

- Kimi 优先的文本模型路由，支持 MiMo API 回退。
- MiMo API 语音转写与流式语音生成。
- Vosk 本地唤醒词监听，不占用显存。
- SQLite 本地记忆、权限、费用和任务记录。
- 可开关的小技能：浏览器、应用、屏幕、音量、定时任务和智能设备等。
- 用户路径、设备信息和令牌均在首次使用时于本机生成。

## 运行

需要 Windows 10/11 与 Python 3.10 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-release.txt
.\.venv\Scripts\python.exe -m assistant_app.qt_quick.app
```

首次使用时可从界面填写 API 密钥，并按需配置唤醒词、技能权限和设备集成。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

## Beta 更新

### v0.0.2_beta1

- 桌面界面已迁移到 Qt Quick/PySide6，并作为唯一图形入口。
- 已接入文字与语音对话、唤醒词、连续对话、托盘、技能、权限、定时任务和集成设置。
- 支持启动组件安装、API 密钥管理、语音引擎选择与本地用量显示。
- 主界面与全部设置页保持统一的圆角卡片布局，并修复侧栏透明、切换残影和预加载状态显示。

### v0.0.1_beta1

- 完成技能栏、技能库、收藏夹和设置入口。
- 支持 API 文本与语音、唤醒词、本地记忆、定时任务及电脑操作。
- 优化预加载、托盘、权限与运行性能。
