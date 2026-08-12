# MaoMao Personal AI Assistant

猫猫是一个更像人的 Windows AI 助手：通用模型负责理解和思考，小技能负责执行具体动作，互动感是第一追求。

当前 `main` 分支对应稳定版本，后续测试版本继续在 `beta` 分支开发。程序默认采用 API 文本、转写和语音生成；记忆、权限、操作记录和个人配置保存在用户自己的电脑上。

当前版本：`v0.0.2`

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

## 版本更新

### v0.0.2

- 桌面界面完整迁移到 Qt Quick/PySide6，并统一圆角卡片、弹窗、选择器和输入控件样式。
- 接入文字与语音对话、唤醒词、连续对话、托盘、技能、权限、定时任务和设备集成设置。
- 支持可验证、可排序的自定义技能分类，以及技能栏、收藏夹和技能归类搜索。
- 优化窄窗口、高 DPI 和快速拉伸布局，使用线程化 OpenGL 避免 Windows 缩放黑边。
- 延迟加载模型客户端、音频设备、NumPy、声纹模板和可选依赖，缩短桌面程序启动路径。
- 优化唤醒声纹 MFCC/DTW、实时录音缓冲、SQLite 迁移、FTS5 记忆检索、缓存和批量写入。
- 加入内容无关的本地性能日志、启动组件安装、安全权限确认和公开包隐私检查。
- 移除旧 GUI 与无用依赖，统一由 Qt Quick 作为图形入口。
