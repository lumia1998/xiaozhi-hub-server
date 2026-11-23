# Xiaozhi Hub Server

面向小智协议设备和 Ubuntu ThinkSmart 带屏音箱的 AI 语音服务端。Hub 负责语音识别、模型与工具调用、语音合成、聊天记忆、后台任务和视觉分析。

配套设备客户端为 [xiaozhi-thinksmart-client](https://github.com/lumia1998/xiaozhi-thinksmart-client)。

### 对话路由与扩展

后台「音箱预览」使用已配对客户端主动建立的控制连接，与聊天 WebSocket 独立。待机仍在线，连接断开后不再计数。登录管理员可查看所选音箱的真实界面、发送文字、按住音箱麦克风、操作已授权家居、编辑音箱本机日程。界面状态与数据来自音箱，不同步两块屏幕的当前导航。关机、唤醒、相机以及修改配对凭据不在预览的 HTTP 转发清单内。客户端配套的 `web/` 需安装到 Hub 的 `data/speaker-web/`（或设置 `THINKSMART_PREVIEW_WEB`）。在同一页勾选/取消 HA 设备卡片后保存，刷新音箱家居面板即生效；未保存布局时沿用原授权实体，保存空布局表示不显示任何卡片。显示卡片不授予设备操作权限。

配对的 ThinkSmart 客户端可通过 `/display/weather` 和 `/display/devices` 获取天气、设备状态；开关操作只接受明确的目标状态，复用 Home Assistant 的设备白名单和读写权限。面板接口要求设备配对，不依赖聊天会话，也不向客户端下发 Home Assistant 凭据。初始清单展示已授权设备所在房间的 HA 注册设备（注册表不可用时回退到授权实体）；管理员保存布局后以所选设备为准。已打开的家居面板会收到刷新通知。跨主机访问模型选择和历史仅允许请求配对身份自己的数据。

- “现在几点”“今天几号”“今天星期几”使用设备上报的时区直接回答；旧设备沿用 `server.timezone_offset`。设置 `direct_clock: false` 可关闭。带地点、条件或其他任务的问题保留给模型。
- 家居直达还需启用对应 Intent 的 `hass_get_state` / `hass_set_state`（或 `hass_state` 模块）。直达和模型的家居状态工具只访问配置中列出的实体；只开查询不能控制设备。
- 每轮只注入已启用 Skill 的目录。模型用只读 `skill_view` 获取正文；`/技能ID` 或“使用出行技能，……”可以显式载入一个技能。不执行技能内的脚本。
- 后台 MCP 编辑器的“允许的工具”使用服务器原始名称。留空保持兼容（全部开放），`[]` 不开放任何工具，`["lookup"]` 只开放 `lookup`。保存后新连接生效。
- 服务端 MCP 首次或配置变更后连接并发现工具；同配置的新会话在十分钟内复用进程内目录，实际调用时才连接并核对工具定义。目录不代表在线，失败的设备操作不自动重试。
- 同名家电不猜目标：2–4 个可区分候选支持卡片或下一句说房间名。选择绑定当前连接、设备归属和模型，最多保留 45 秒；打断、换话题后失效。成功确认目标后可接“再暗一点”；结果未知时“刚才成功了吗”只查状态，不重发控制命令。
- 性能日志 `turn_latency` 仅包含随机轮次 ID、阶段、毫秒数。将 Hub 和音箱的日志导出后运行 `python test/latency_report.py hub.log speaker.log`，可按直达、普通模型、MCP 调用是否需要建连分别查看样本数及 P50/P95。每侧使用单调时钟，不相减跨机时间戳。`asr` 包含说完后的识别及声纹等待，`model_first_text` / `first_opus_sent` 从 Hub 开始路由计时；`speech_end_to_output` 从客户端最后检测到语音到首段回复 PCM 进入扬声器输出并加上驱动报告的 DAC 延迟，不是麦克风实测的声学首声。文字提问不会混入这个语音指标。

```text
ThinkSmart / 小智协议设备
        ⇅ WebSocket JSON + Opus
Hub 语音与会话服务
        ⇅ Pi Agent、设备 MCP、服务端工具
模型服务 / 视觉模型 / 外部应用
```

## 功能

| 功能 | 当前实现 |
| --- | --- |
| 语音对话 | 可配置 ASR、VAD、TTS 提供商，流式音频发送和打断处理 |
| 模型服务 | 管理接口地址与凭据、获取模型列表、检测协议、选择默认模型和配置人设 |
| 多模型会话 | 按称呼切换模型，按设备与模型保存对话，维护上下文窗口 |
| 工具调用 | Pi Agent 对话循环、设备 MCP、服务端 MCP、插件及模型前置处理 |
| 设备控制 | 调用客户端公开的音量、亮度、定时亮度及摄像头工具 |
| 视觉分析 | 接收图片与问题，通过配置的视觉提供商返回识别文字 |
| 多媒体输出 | 为支持媒体协议的设备提供 `show_media` 工具，发送图片、视频和音频直链 |
| 记忆 | 对话持久化、摘要、用户偏好，以及按需调用的共享记忆接入 |
| 后台任务 | 任务提交、状态查询、取消、补充回答及结果投递 |
| 管理后台 | 模型、音色、插件、会话、记忆、任务、设备配对和运行状态 |

模型服务支持检测 OpenAI Chat Completions、OpenAI Responses、Gemini 和 Claude 协议，实际可用能力取决于所配置的接口与模型。说明见 [模型服务与人设](hub-docs/model-services.md)。

## 运行环境

生产部署以 Linux 为主。核心服务使用 Python，Pi Agent 运行时需要 Node.js **22.19 或以上版本**。音频链路依赖系统 FFmpeg、libopus 及所选提供商的运行组件，离线 ASR 还需对应模型文件。

仓库保留 macOS/Linux 与 Windows 安装脚本。测试环境与生产虚拟环境分别管理，详见 [test/README.md](test/README.md)。

## 安装与启动

### 1. 获取代码并准备基础环境

```sh
git clone https://github.com/lumia1998/xiaozhi-hub-server.git
cd xiaozhi-hub-server
bash scripts/install.sh
```

安装脚本准备 Python 环境、基础依赖、默认语音识别模型及初始配置。已有配置会保留。Windows 对应入口为 `scripts/install.ps1` 和 `scripts/start.ps1`。

### 2. 准备 Pi 运行时

安装 Node.js 22.19 或以上版本后执行

```sh
npm ci --omit=dev --omit=optional --prefix main/xiaozhi-server/core/providers/llm/pi/runtime
```

### 3. 配置并启动

运行配置位于 `main/xiaozhi-server/data/.config.yaml`，与仓库默认 `config.yaml` 合并使用。启动入口为

```sh
bash scripts/start.sh
```

也可在已准备好的运行环境中直接启动

```sh
cd main/xiaozhi-server
.venv/bin/python app.py
```

打开 `http://127.0.0.1:16688/admin` 设置管理员密码，在“大模型”页面添加模型服务、检测协议并选择默认模型，再配置 ASR 和 TTS。安装脚本生成的初始模型项需要结合实际凭据调整。

## 连接设备

| 地址 | 用途 |
| --- | --- |
| `http://<Hub 地址>:16688/admin` | 管理后台 |
| `http://<Hub 地址>:16688/xiaozhi/ota/` | 小智固件 OTA 与连接信息 |
| `ws://<Hub 地址>:16689/xiaozhi/v1/` | 语音 WebSocket |
| `http://<Hub 地址>:16688/mcp/vision/explain` | 带认证的图片视觉分析 |

ThinkSmart 客户端可在设置页填写 HTTP 地址和 WebSocket 地址。启用 `server.auth.device_auth.enabled` 后，需要使用后台签发的设备配对令牌，客户端通过 `X-Device-Pairing-Token` 提交。配对与 HMAC 认证是否强制执行由运行配置决定。

`/kiosk/*` 为本机来源限制接口，用于模型选择、历史记录及部分辅助功能。分开部署客户端与 Hub 时，这些接口需要部署层提供受控的本机转发，普通局域网直连会收到 403。语音 WebSocket 和视觉上传使用各自的连接与认证流程。

## 图片、视频与摄像头

### 向设备展示媒体

设备通过 `hello.features.media` 声明能力后，Hub 向模型开放 `show_media`。工具接收 `image`、`video`、`audio` 类型及可访问的 HTTP(S) 直链，通过 JSON 消息交给客户端展示或播放。

媒体结果随工具历史持久化，设备读取聊天记录时可恢复媒体条目。客户端负责资源加载、播放控制及解码，资源链接的有效期由来源服务决定。详细消息格式见 [媒体输出协议](hub-docs/media-output.md)。

### 查看摄像头画面

支持摄像头的客户端注册 `self.camera.take_photo`。用户提出“看看前面是什么”“读一下眼前文字”等请求时，模型调用设备工具，客户端按需采集图片，使用 MCP 初始化下发的视觉令牌上传，再将识别结果返回对话。

Hub 视觉配置示例

```yaml
selected_module:
  VLLM: CameraVision

VLLM:
  CameraVision:
    type: openai
    model_name: your-vision-model
    base_url: https://api.example.com/v1
    api_key: your-api-key
```

将示例字段合并到已有配置并填入实际视觉模型。该接口接受问题和图片的 multipart 上传，图片上限为 5 MB。ThinkSmart 的摄像头驱动、设备树和 libcamera 属于设备系统层依赖。

## 数据与配置

| 路径 | 用途 |
| --- | --- |
| `main/xiaozhi-server/data/.config.yaml` | 私有运行配置及模型服务设置 |
| `main/xiaozhi-server/data/chat.db` | 聊天、记忆及相关持久化数据 |
| `main/xiaozhi-server/data/device_auth.db` | 设备配对记录 |
| `main/xiaozhi-server/core/agent/` | 对话循环、记忆、任务和模型路由 |
| `main/xiaozhi-server/core/providers/` | 模型、音频与工具提供商 |
| `main/xiaozhi-server/core/admin/` | 管理后台及接口 |
| `test/` | 测试、独立依赖清单和隔离配置 |

私有配置、数据库、令牌、日志及测试缓存应保存在各自部署环境中。备份时保留私有配置和数据库，升级后重启实际部署所使用的服务。

## 测试

在仓库根目录执行

```sh
python3 -m venv test/.venv
test/.venv/bin/python -m pip install -r test/requirements.txt
PYTHONDONTWRITEBYTECODE=1 test/.venv/bin/python -m pytest -c test/pytest.ini -q
node --test test/test_models_page_state.cjs
```

默认测试使用隔离配置和模拟外部服务。真实 API 测试通过 `RUN_LIVE_API_TESTS=1` 启用，Pi 集成测试的运行时准备步骤见 [测试说明](test/README.md)。

## 文档

- [模型服务与人设](hub-docs/model-services.md)
- [语音合成提供商](hub-docs/tts-providers.md)
- [模型前置处理](hub-docs/pre-llm-hooks.md)
- [媒体输出协议](hub-docs/media-output.md)
- [ThinkSmart 客户端](https://github.com/lumia1998/xiaozhi-thinksmart-client)

`hub-docs/guide.md`、`hub-docs/design.md` 和英文旧版说明保留了早期架构背景，当前功能与入口以本页及对应专项文档为准。

## 来源与许可

[MIT License](LICENSE)。基于 [justhalfbit/xiaozhi-hub-server](https://github.com/justhalfbit/xiaozhi-hub-server) 和 [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 继续开发。

核心运行目录为 `main/xiaozhi-server`。仓库同时保留上游管理端、数字人及其他组件，使用这些组件时请查阅对应目录的说明。
