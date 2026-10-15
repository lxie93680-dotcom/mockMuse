# Muse 浏览器 Demo

参考 Meta Muse 的「云端 Agent + 独立浏览器 + Web 控制入口」形态实现的单用户 Demo，不是 Meta Muse 的源码或完整复刻。

技术栈：Python / FastAPI / OpenAI Agents SDK / Kimi、DeepSeek 或自定义兼容模型 / Chrome 扩展（Manifest V3）。Agent、浏览器和 Web 服务部署在同一台机器；本地通过 Web 访问。通过 Chromium 的 CDP Screencast 持续传输浏览器画面，目标上限约 20 帧/秒；静止页面不会重复发送，实际速度受页面绘制与网络影响。不包含整个操作系统桌面。

## Chrome 扩展连接

1. 在 Chrome 打开 `chrome://extensions`，开启开发者模式。
2. 点击“加载已解压的扩展程序”，选择项目 `extension/` 文件夹。
3. 打开要操作的 HTTP/HTTPS 网页，点击“Muse 浏览器连接器”。
4. 输入 `http://127.0.0.1:8000` ，点击“连接 / 恢复自动控制”。
5. Chrome 会显示调试提示。工作台开始显示该页实时画面，Agent 和人工接管都走扩展。
6. 点击“断开控制”或取消 Chrome 调试会停止控制，并取消正在执行的任务；关闭受控页后，下次操作自动选择其他网页。

扩展仅连接本机回环地址，无需口令，API Key 仍留在服务端。云端扩展也连接云电脑本机 localhost；工作台的远程访问仍保留鉴权。扩展通过官方 `chrome.debugger` 传输 CDP 操作和 Screencast，没有 Playwright 依赖，也不修改浏览器自动化指纹。此方案不保证消除网站验证。

扩展加载及 Chrome 启动时默认自动连接本机工作台；服务重启时自动重连。主动断开或取消 Chrome 调试后暂停自动控制，点击扩展中的连接按钮恢复。第一版不处理跨域 iframe 的文本引用，截图仍可见。可操作所有 HTTP/HTTPS 标签页，无需逐页授权；Agent 使用 list_tabs/switch_tab 发现并切换页面，open 在没有受控页时创建网页。Chrome 内部页面不支持控制。

官方依据：[Chrome debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger)、[扩展 WebSocket 生命周期](https://developer.chrome.com/docs/extensions/how-to/web-platform/websockets)。

## 本机运行

需要 Python 3.11+ 和 uv。使用 asyncio 事件循环（本机验证中 uvloop 导致浏览器页面加载超时）：

```sh
uv sync
cp .env.example .env
# 编辑 .env：填写所选供应商的 API 密钥、模型名和访问口令
uv run uvicorn muse.app:app --host 127.0.0.1 --port 8000 --loop asyncio
```

打开 http://127.0.0.1:8000 。本机工作台与 Chrome 扩展免口令连接，仅限回环地址访问；远程访问仍使用访问口令。密钥只在服务端 `.env` 中配置，浏览器不会接收密钥。没有所选供应商的 API 密钥也能点击「运行浏览器演示」，验证真实操作、截图和接管；这是明确标记的固定流程，不是模型执行。

真实模型任务示例：`打开 https://example.com，读取并用中文总结页面内容。`

## 无影 Windows / Linux 部署

1. 将项目复制到云电脑，安装 Python 3.11+ 和 uv，运行 `uv sync`。
2. 安装普通 Chrome，按下方扩展安装步骤加载项目的 `extension/` 目录。
3. 创建 `.env`，填写 `KIMI_API_KEY`、`KIMI_MODEL`（或对应供应商配置） 和随机 `MUSE_ACCESS_TOKEN`。
4. 在云电脑的图形桌面中启动服务、打开 Chrome，在扩展中填写云电脑本机工作台地址并连接。保持桌面会话和 Chrome 运行。
5. 服务默认只监听本机：`uv run uvicorn muse.app:app --host 127.0.0.1 --port 8000 --loop asyncio`。
6. 从你的本地电脑建立到云电脑的通道。无影客户端的桌面连接不等于端口访问，需另行验证网络。

如果云电脑可通过 SSH 到达：

```sh
ssh -L 8000:127.0.0.1:8000 USER@CLOUD_HOST
```

然后本地打开 http://127.0.0.1:8000 。如果没有入站地址，可以使用双方可达的私有组网，或让云电脑通过反向 SSH 隧道连接你有权限的中继服务器：

```sh
# 在云电脑运行；中继端口只绑定中继本机
ssh -N -R 127.0.0.1:18000:127.0.0.1:8000 USER@RELAY_HOST
# 在本地运行
ssh -N -L 8000:127.0.0.1:18000 USER@RELAY_HOST
```

这两种方式不需要企业版 Web SDK。网络或隧道断开不会取消已开始的 Agent 任务，重新连接后恢复查看；云电脑必须保持开机，关闭断连自动关机/休眠策略。如果服务进程退出，任务不会自动续跑。

若使用自建 HTTPS 反向代理，需转发 WebSocket 并保留 Host，服务需正确识别代理的 HTTPS 协议。不要把开发服务直接暴露为无认证的公网端口。

## 使用与边界

- Web 页面显示当前浏览器实时画面；停止 Agent 并接管后，可直接点击、拖动、滚轮、键盘输入，也可在接管区的地址栏导航。鼠标按下、移动和松开分别通过 WebSocket 发送，不再等松开后整体回放拖动。中文输入及粘贴通过键盘代理传递。
- 所有 HTTP/HTTPS 标签页可按任务自动附加调试控制，同一时间控制一个页；切换后必须重新观察，不复用其他页的元素引用。新窗口通过 list_tabs/switch_tab 操作。
- 单用户、单任务执行；浏览器登录态使用普通 Chrome 自己的用户配置，聊天与日志保存在 SQLite。重启后保留浏览器登录态和记录，但模型上下文不会恢复，进行中的任务标记为中断。
- 模型通过 Chat Completions 适配器接入，关闭 OpenAI tracing，不需要 OpenAI API Key。模型名称可配置；真实 API 兼容性须使用有效的供应商 API 密钥验证。
- 浏览器工具支持打开、读页面、点击、填写、选择、按键和滚动。文本观察最多 16000 字符、120 个交互元素；复杂 iframe 和跨页下载不在第一版范围内。地图控件支持截图理解、坐标点击和按文字点击；验证码由用户停止 Agent 后手动接管完成，Agent 不自动处理。
- Demo 在提示词里限制付款、发布、删除等动作，这不是系统级安全边界。只用于公开资料与测试网页；不具备 Meta Muse 的 Sentinel、凭据隔离或安全 VM 能力。
- 网络、无影休眠策略、Chrome 扩展策略和图形会话需要在你的实例上验收。

## 验证

```sh
uv run python -m unittest discover -s tests -v
```

自动测试覆盖扩展连接鉴权、命令往返、断线取消、画面尺寸、Web 接管排他、模型适配和持久化；使用模拟扩展，不需要模型密钥。真实 Chrome 扩展须安装后手动联调：用 `/control-test` 验证点击、输入、拖动与滚轮，检查控制取消后停止。

## 切换模型

默认 `LLM_PROVIDER=deepseek`，DeepSeek 模型为 `deepseek-flash`（当前官方映射 DeepSeek-V4.1-Flash）；Kimi 可切换使用。密钥填写在服务端 `.env` 的 `KIMI_API_KEY`（也支持 `MOONSHOT_API_KEY`）。DeepSeek 使用 `DEEPSEEK_API_KEY`，两组配置可以同时保留。

Web 输入框下方的供应商下拉框可切换 Kimi / DeepSeek / 自定义接口，切换供应商不需要重启。执行期间不能切换；请先停止任务。切换后重置模型对话上下文，浏览器登录态及聊天展示记录保留。刷新网页保留当前进程的选择；服务重启后以 `LLM_PROVIDER` 为默认供应商。

自定义接口填写 `CUSTOM_API_KEY`、`CUSTOM_BASE_URL`（通常含 `/v1`）、`CUSTOM_MODEL`；必须支持 OpenAI Chat Completions 和 function calling。兼容层不保证所有模型均支持工具调用，需要针对供应商实测。

修改 `.env` 的密钥、模型、API 地址或额外参数后需重启服务。各供应商支持 `*_EXTRA_BODY` JSON 对象配置；自定义接口默认不发送 `thinking` 等特定参数。Kimi K2.5/K2.6 和 DeepSeek 默认使用非思考模式；Kimi K2.7 Code 等模型保留其默认思考模式，并在多轮工具调用中回传同一模型的 reasoning_content。

官方参考：[Kimi API](https://platform.kimi.com/docs/get-api-key)、[Kimi K2.6 参数](https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart)、[DeepSeek 工具调用](https://api-docs.deepseek.com/guides/tool_calls/)。

远程控制验证页：在接管区地址栏打开 `http://127.0.0.1:8000/control-test`，可测试直接打字、Enter、拖动方块和滚轮。一次只允许一个 Web 页面接管；断线后自动释放控制。
## macOS 快速启动

完成首次安装和 `.env` 配置后，双击项目中的 `启动Muse.command`。
脚本会启动本地服务并自动打开工作台；如果服务已在运行，就直接打开工作台。
保持启动脚本的终端窗口开启，按 `Control+C` 停止服务。
也可在项目目录运行 `./启动Muse.command`。启动工作台后，Chrome 扩展默认自动连接本机工作台；服务不再自动启动独立浏览器。


## Linux 文件与终端执行端

Agent 新增 `linux_files`（list/read/write/mkdir/move）和 `linux_terminal`（Bash、退出码、stdout/stderr、1–60秒超时）。它们只调用独立 Linux 服务，不使用 Agent 宿主机的终端或文件系统，没有本地回退。未配置服务时明确报错。纯文件任务无需连接浏览器。

在**云端 Linux**安装 Docker 与 Compose 后，在项目目录执行：

```sh
# 生成独立执行端口令，不要与工作台的 1234 口令共用
export MUSE_LINUX_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
docker compose -f deploy/compose.linux.yml up -d --build
```

将同一口令配置到云端 Muse `.env` 的 `MUSE_LINUX_TOKEN`，并设置 `MUSE_LINUX_URL=http://127.0.0.1:8765`，然后启动或重启 Muse。Compose 运行时也需要这枚口令；将其保存在仅部署用户可读的环境文件中，后续使用 `docker compose --env-file 文件路径 -f deploy/compose.linux.yml up -d`。不要将口令提交到代码仓库。

文件持久化在 Docker 的 `linux-workspace` 命名卷内，对应容器 `/workspace`，没有挂载本地 Mac 目录、Docker socket、模型密钥或云主机个人目录。终端以非 root 运行，容器根目录只读，限制内存、进程、CPU；工作目录与 /tmp 可写。Bash 可以访问容器内其他可读文件并使用网络，文件相对路径检查不是 shell 沙箱，容器才是隔离边界。不要在该容器放凭据。终端超时和退出后清理同进程组子进程，不用于后台常驻服务。文本读写和输出上限256 KiB；不提供独立递归删除工具。

端口仅绑定云主机回环地址。跨机器使用 HTTPS 或 SSH 端口转发，不能直接公开该终端服务。执行端在非 Linux 系统拒绝启动。文件测试用临时目录和模拟平台，不执行本地终端；真实 Bash、容器资源限制、持久化和云端 Chrome 图形会话需部署后验收。

## 本地文件工具

`local_files` 支持 list/read/write/mkdir/move，默认工作目录为项目 `data/workspace/`；可通过 `MUSE_LOCAL_WORKSPACE` 指定独立目录。路径使用相对路径，拒绝越界、隐藏文件和符号链接，不提供删除工具。文本限256 KiB，写入默认拒绝覆盖，修改现有文件需明确指定 overwrite。此工具访问运行 Muse 服务的电脑，不调用 Linux 执行端，也不执行本地终端。示例：`在本地工作目录创建 notes/计划.txt，写入明天的任务，然后读取确认。`

## 本地 macOS 模式新增工具

当前 Agent 仅注册本地文件/文档/终端和浏览器工具，不暴露 Linux 远程工具。

- `browser_crop_image(x,y,width,height,scale)`：按 browser_view 原图像素坐标裁剪当前截图并发送聊天，scale为1–4。无需更新Chrome扩展。必须先看截图确认位置；放大不会增加细节。
- `local_files` 新增 search（query搜索文件名/文本，最多100条结果）与 edit（将唯一query替换成content）。现有列目录、读写、建目录、移动保留。不提供删除工具。
- `local_terminal(command,cwd,timeout)`：macOS使用 `/bin/zsh -f`，超时1–60秒；输出各限64 KiB；返回退出码与超时状态。工作目录默认 `data/workspace`，不继承服务模型密钥、不加载用户shell配置。以当前用户运行，**并非沙箱**，shell仍可访问用户有权限的目录。停止任务会终止同进程组子进程，不用于后台常驻服务。
- `local_documents(action,path,content,overwrite)`：读取和创建 `.pdf`、`.docx`、`.xlsx`。生成Word/PDF使用纯文本，Excel使用二维JSON数组。读取文档限20 MiB；PDF最多30页，Excel最多10表、每表200行30列；文本结果限100000字符。不支持扫描件OCR、宏、Excel公式计算或旧版doc/xls格式。生成文档是基础内容布局，不能承诺出版级排版。

绝对文件路径需在 `.env` 的 `MUSE_LOCAL_ALLOWED_FILES` 内逐个授权，用系统路径分隔符分开。当前桌面hello.txt是单独授权，不等于开放整个桌面。

## Desktop Commander + Filesystem 本地 MCP

当前本地配置启用两套开源 stdio MCP，随 Muse 启动/退出自动创建和关闭 Node 子进程。无需 HTTP 端口、OAuth 或手动启动第二个终端。

```sh
npm ci --prefix vendor/desktop-commander --ignore-scripts
npm ci --prefix vendor/filesystem --ignore-scripts
# .env：MUSE_DESKTOP_COMMANDER=1 和 MUSE_FILESYSTEM_MCP=1
```

版本固定为 Desktop Commander 0.2.52、Filesystem 1.0.0；依赖锁文件保存在各 vendor 目录。Node.js需要18+。首次安装需要网络；启动时使用本地安装包，不在线运行npx。安装跳过依赖脚本；需要额外原生组件的文档能力需另行验证。

初始化后调用 tools/list 并缓存。SDK配置 include_server_in_tool_names，模型看到 `mcp_Desktop_Commander__...` 与 `mcp_Filesystem__...`，避免工具重名。基础文件任务优先 Filesystem；Desktop Commander只暴露终端会话及PDF/Excel相关读写共8项工具；配置权限、遥测、任意PID终止等工具不提供给模型。内置local_files/local_terminal不重复暴露，local_documents保留支持Word及其他文档回退，浏览器与区域截图工具保留。Chrome DevTools MCP未接入，浏览器仍通过现有扩展控制。

工作目录为data/workspace。Desktop Commander独立HOME/config/log位于data/mcp-home，遥测关闭，子进程不继承模型密钥。Filesystem允许目录与Desktop Commander文件目录一致。之前单独授权的桌面hello.txt不会自动扩大成整个桌面权限，也不会自动成为Filesystem的授权目录。终端仍以当前macOS用户权限运行，文件目录限制不是终端沙箱。工具调用及返回状态写入Muse执行记录；MCP初始化失败会显示错误，不静默回退原工具。

资料：[Desktop Commander](https://github.com/wonderwhy-er/DesktopCommanderMCP)、[Filesystem MCP](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem)。

## 执行循环限制与预警

`MUSE_MAX_TURNS=100` 控制每任务最多模型轮次，支持1–1000。工具报错按工具全名+标准化入参+错误返回值三者连续相同计数，连续3次停止；换工具、入参或错误返回值后重新计为1，中间任意工具调用成功则清零，成功返回重复不计失败。异常返回中的tool_call_id仅是关联元数据，不参与错误内容比较，原始错误仍按调用ID保存。结构化非零exit_code/timed_out、MCP is_error/isError和未执行的浏览器定位恢复也按失败结果比较；不猜测业务文本是否失败。

JSON对象键排序、JSON字符串解析、文本换行统一并去掉首尾空白；数组顺序和实际返回内容保留。计数日志只记录工具名和次数。模型请求仍采用每任务累计失败3次上限（成功不会清零），永久余额/鉴权错误立即停止；模型错误由Muse层重试，OpenAI客户端max_retries=0。

每任务总处理时长固定为600秒，通过asyncio.wait_for限制整个execute流程。使用事件循环的单调时钟，包含模型等待、工具执行和重试，不受系统日期调整影响；不是把各工具耗时累加。超时取消Agent执行并记录停止原因；已提交的页面修改和正在远端执行的操作不会自动回滚。

## 执行阶段状态

任务整体status保留idle/running/completed/stopped/error/interrupted；运行期间另有phase：thinking（准备任务和整理下一步上下文）、replying（等待模型响应，含推理耗时）、executing_tool（实际调用工具）、checking_result（检查返回、错误、重复及最终回复有效性）。phase_detail和active_tools通过WebSocket显示到工作台。并行工具全部返回前保持executing_tool；停止、错误、完成和会话切换清空phase。阶段不展示模型内部思维，也不代表独立业务验收；当前completed仍是模型执行结束。模型请求目前非流式，replying不会逐字输出。

## Debug模式

页面右上角Debug按钮打开浮动调试窗口，仅开启后采集模型调用。每次调用显示当前user输入、assistant回复或工具调用；历史消息、请求JSON和返回JSON默认折叠，展开状态在实时刷新中保留。请求是Agents SDK传给模型适配器的参数，返回是SDK的ModelResponse，不是完整HTTP报文。工具schema包含在请求中；服务鉴权头与API Key不采集。嵌入图片省略，长字符串截断，每次保留最近30条；日志仅在内存，不写SQLite，关闭或清空后删除。Debug模式为当前单用户工作台共享状态，已登录的其他页面也能看到；会话和工具内容可能含用户信息，按原始请求内容展示。

## delegate_task 子Agent委派

主Agent新增 `delegate_task(task,role,context)`，role仅支持researcher/verifier，由工具内部启动独立Agent并返回status/role/result。子Agent只接收委派任务与显式context，不继承主会话历史，也不把内部对话写入主Session；主会话保存委派调用和返回结果。

权限由代码固定：Filesystem只读工具+只读浏览器信息/截图，不能写文件、操作浏览器、运行终端、修改权限或再次委派。主Agent关闭并行工具调用，委派工具额外串行锁，避免主子Agent同时修改页面。子Agent只读浏览器观察可能更新页面ref，主Agent后续操作前应重新read。每个子任务最多20模型轮次、120秒，包含在主任务600秒内；工具报错共享主任务连续相同3次限制；模型错误共享累计3次上限。模型使用当前主任务供应商；日志显示子Agent开始、调用和完成。completed表示子Agent正常返回，具体是否验证通过需看result，不能代替独立业务验收。

### 通用SubAgent（取代固定角色）

委派接口现在不使用role：`delegate_task(task,context,instructions,tools,max_turns,timeout_seconds)`。主Agent先通过`list_subagent_tools`获取后端可委派能力列表，自行选择工具、生成instructions并整理必要context。instructions与context均由主Agent提供，不自动继承全历史。后端固定权限边界不随instructions改变；tools省略/null继承过滤后的能力，[]无工具（适合压缩），显式名称从列表选择；未知或禁止工具报错。每次返回实际tools清单，预算限制1–20轮、1–120秒，不支持递归。旧verifier/researcher角色选择已移除。

### 子Agent工具继承策略更新

子Agent改为父Agent当前工具集减去固定DENIED_TOOLS，不再使用只读工具白名单。新增父工具默认同步继承，调用方tools只进一步缩小范围。MCP工具按基础名匹配排除规则。当前排除递归、终端、浏览器修改、文件写入及混合读写入口；browser_action由read_browser替代，browser_view直接继承。新增工具若不宜委派，必须更新后端排除列表；黑名单不保证未知新工具一定只读。子任务默认及最高轮次为100，120秒上限不变，主任务30分钟时限继续生效，工具采用连续相同报错3次限制，模型采用累计失败3次限制。

### 自动上下文压缩与 checkpoint

每次主 Agent 请求模型前检查上下文压力（历史、system instructions、工具定义和图片预算）。Kimi K2 默认窗口 262144 tokens，达到 50% 时触发独立的无工具压缩 SubAgent。保留最近至少 20000 tokens 的完整消息，同时保护最新用户指令与工具调用/结果配对。边界消息可能使保留量超过 20000。其他模型通过 `MUSE_CONTEXT_WINDOW` 配置容量；未知容量时不自动压缩。模型参数按 `config/model_contexts.json` 的供应商与完整模型名精确匹配。厂商返回的 usage 是实际用量；相同输入沿用实际输入 tokens，新增内容用上次实际值加模型配置的估算增量。首次调用、system/工具定义变化时使用明确标注的兜底估算。最近保留范围仍使用估算，不声称是 Kimi 精确 tokenizer。

第一次压缩输入是较早原文；后续输入是上一轮累计摘要与随后移出保留区的原文。压缩子 Agent 不继承主 Agent 全上下文、不带文件/浏览器工具，共享三次模型失败限制与整轮十分钟时限。压缩失败保留原文，本轮不重复尝试；成功后立即持久化 checkpoint 与有效模型历史。SDK 返回的旧原文不会覆盖压缩后的上下文。

持久化位置为 `data/muse.db`：`context_items` 保存原始消息/摘要，`context_checkpoints` 保存历史 checkpoint，`context_latest` 保存每个会话的最新指针和有效上下文；`context_usage` 保存模型校准基准，`context_usage_events` 保留实际用量历史。普通消息无供应商 ID 时，后端分配稳定 message ID；摘要 ID 使用后端 `generate_checkpoint_item_id()` 工具生成。未压缩时 context_summary 为 null，最新 checkpoint 对应最新消息。压缩后最新 checkpoint 对应累计摘要，近期原文在 snapshot 中继续保存。`GET /api/context/checkpoint`（需登录）查看当前会话 checkpoint。

```json
{
  "item_id": "summary_后端UUID",
  "kind": "summary",
  "content": "本次生成的累计摘要正文",
  "context_summary": "本次生成的累计摘要正文",
  "covered_through_item_id": "摘要覆盖到的最后一条原文ID",
  "created_at": 1791850000.0
}
```

### 结构化消息存储（当前）

`messages` 是历史检索的标准消息表：独立的 `message_id/session_id/task_id/turn_id/api_request_id/role/content/tool_calls/tool_call_id/created_at/kind` 字段。同一模型响应的正文与调用合并为一条 assistant 消息，`tool_calls` 是 JSON 数组；每条工具返回是独立 tool 消息。`messages_fts` 是 FTS5 trigram 索引，由数据库插入/更新/删除触发器维护，支持中文连续片段；少于3字符使用子串查询。`search_history` 返回 `message_id`，`read_history(message_id, offset, max_chars)` 分页读取，限制当前会话。

`task_id` 贯穿一次运行生命周期；模型成功返回后建立本次响应处理的 `turn_id`；`api_request_id` 按逻辑调用序号生成 `task_id:api:N`，网络重试共用ID，`retry_index` 单独记录。`task_runs` 与 `model_requests` 保存追踪记录。SDK的历史数组和 `context_items` 是模型重放及压缩的内部格式，不作为工具查询的消息接口；`message_items` 映射内部重放项与标准消息。checkpoint 的 `covered_through_message_id` 指向标准消息边界。前述旧 `item_id` 检索接口已移除。

### 每次工具返回的上下文处理

`muse/tool_results.py` 先存完整工具原文，再生成模型可见投影。网页HTML清理后只传标题、URL及相关片段；浏览器元素ref作为操作元数据保留。其他超过15000字符的文本结果保留头尾（含JSON信封仍不超过15000字符），通过source_message_id读取完整原文。图片输出保留类型不做文本截断。Chrome扩展的HTML采集改动需要在chrome://extensions重新加载扩展。

调优记录维护在桌面 `/Users/yue/Desktop/Muse调优记录.txt`；后续调优追加问题、改动、验证、实际效果和限制，未测量结果不写成提升结论。本次合成网页的Kimi实际usage对照见 `data/tool_result_benchmark.json`，不能当作真实网页的平均节省率。

浏览器定位恢复：click_text文字不唯一、元素失效或被遮挡时，后端仅在确定尚未发送输入的定位阶段重新读取页面，返回status=needs_observation、action_performed=false和最新元素。模型应重新选择目标，不自动重放点击；定位恢复表示操作未执行，按连续相同报错规则计数；重新选择目标、更换返回值或后续操作成功会重置计数。重新读取失败、连接故障、其他工具错误也按工具名称+入参+错误返回值连续相同3次停止，不再累计不同错误或成功返回。

## Playwright MCP 浏览器操作（当前默认配置）

本机 `.env` 配置 `MUSE_BROWSER_BACKEND=playwright`。通过固定版本0.0.83的MCP执行可访问性快照、点击、悬停、填写、选项和按键；引用使用MCP原生字符串，不由Muse重编号。`browser_action`接口保持，read支持ref局部快照，list_tabs/switch_tab按MCP标签索引操作。Chrome扩展仅负责同一浏览器的实时画面、滚动和人工接管。

依赖安装：`npm --prefix vendor/playwright ci`。配置Chrome for Testing可执行文件`MUSE_PLAYWRIGHT_EXECUTABLE`，默认查找本机Playwright缓存。专用Chrome默认有窗口，使用`data/playwright-profile`持久配置，保存之后的登录状态；不会导入普通Chrome的现有登录。应用退出时关闭自有浏览器，配置保留。远程调试端口由浏览器随机分配，在本机使用。普通Chrome原Muse连接器需断开，避免抢占画面连接。

`MUSE_PLAYWRIGHT_CDP_ENDPOINT`可选择外部管理的浏览器；此时不启动或关闭该浏览器。当前实施的是用户选定的专用持久化Chrome方式。失败时返回原始MCP错误并显示未就绪，不静默退回旧DOM点击。回滚设置`MUSE_BROWSER_BACKEND=extension`并重启，再在普通Chrome恢复连接。运行全部测试可使用`MUSE_BROWSER_BACKEND=extension .venv/bin/python -m unittest discover -s tests`，旧扩展测试显式选择旧后端，新MCP适配器用模拟接口做回归。

## 人工接管：验证码、登录与安全确认

Playwright可访问性快照检测到可见的滑块/真人验证文字时，抛出专用人工操作信号，任务进入`needs_human`，停止自动操作，不算工具失败；当前页面和任务进度保留。模型遇到登录、未覆盖的验证或其他必须人工确认的步骤时，调用`request_human(reason)`，使用相同流程。

工作台显示醒目提醒、“等待你操作”状态和“已处理，继续”按钮，并自动申请当前窗口接管权限；另一窗口已接管时仍保持原有排他控制。没有打开工作台时，等待状态与原因保留，打开或重新连接工作台后申请接管。处理完成后由用户点击继续，后端先重新读取页面；验证码仍在则返回409并继续等待，不调用模型、不解除等待。验证已解除才释放人工控制并启动新的续接轮次，重新观察已有进度，避免重复修改。等待期间不自动运行或自动解决验证码。

专用Chrome退出前保存当前HTTP/HTTPS页面URL到私有profile目录，重启时恢复这些页面；登录仍由持久化profile保存。该文件可能含私有页面URL，不应提交或分享。

检测基于可访问性文字，不保证识别纯图像验证码；此类情况由模型的`request_human`补充。真实滑块验证没有自动处理。本机合成验收：`.venv/bin/python evals/browser_grounding/verify_handoff.py`，验证实际工作台自动接管、未解除验证拒绝续接、人工确认后恢复和重启页面恢复；其中Agent为模拟实现，未调用模型、未访问真实验证码服务。

## 普通 Chrome 的 Playwright 官方扩展连接

设置`MUSE_PLAYWRIGHT_CONNECTION=extension`后，MCP以`--extension`连接现有普通Chrome，不启动或关闭Chrome for Testing，也不迁移/复制普通Chrome的cookie。安装微软官方Playwright Extension： https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm 。首次连接由官方扩展界面选择可控标签组；不同客户端按标签组隔离。`MUSE_PLAYWRIGHT_PROFILE_DIR`可显式指定Chrome配置目录名。默认保留官方连接确认，不自动读取扩展认证Token或绕过确认。

此模式的截图和人工鼠标键盘操作也使用同一个MCP连接，旧Muse扩展无需附加同一页面，避免两个扩展争用调试目标。模型仅获得既有browser_action等封装工具，不提供任意运行JS工具；内部browser_run_code用于固定的人工输入/URL读取。画面采用每秒一次轮询截图，CSS像素坐标；不宣称保持原扩展Screencast帧率。浏览器关闭或扩展断开仍需恢复连接。

代码模式已实现，安装权限确认与真实普通Chrome联调完成前，不将生产.env切换，也不宣称接入完成。官方文档：https://github.com/microsoft/playwright/tree/main/packages/extension 。此连接方式不保证消除网站验证码或风控。


浏览器诊断：`/api/tool-events` 的 `browser_stage` 事件关联同一 session/task/turn/API/tool_call。记录浏览器锁等待/获得/释放、MCP调用dispatch/每5秒pending/returned/error/cancelled及耗时、内部工具名、目标ref、超时配置、最后连接状态和页面错误证据。`rpc_dispatch` 表示进入SDK调用，不能断言数据已经发到扩展；没有底层回包时仍不能区分SDK排队、传输、扩展或页面内部阻塞。后台截图失败保存在browser_driver.stream_error，最后RPC阶段保存在browser_driver.last_rpc；日志不主动读取Cookie、认证Token或额外页面内容。

本地MCP深层诊断通过 `vendor/playwright/diagnostic-hook.cjs` 在加载已安装Playwright bundle时插入日志，不直接修改node_modules，npm ci后仍有效。插入点数量不匹配时明确拒绝启动，避免依赖升级后静默失效。内部阶段覆盖JSON-RPC请求到达、backend等待/就绪、工具处理器、扩展连接等待、协议发送/回包与连接关闭；AsyncLocalStorage及_meta.muse_trace_id关联Python调用。只记录阶段、协议方法名、ID和错误标记，不记录协议参数/结果、Cookie、扩展Token。每次MCP进程的JSONL文件位于data/playwright-output/mcp-trace-*.jsonl，权限0600，最多约4MiB后停止追加；长期诊断需重启生成新文件。Debug开启后显示最近200条事件，错误卡片红色；关闭/清空仅清界面缓存，不删除持久化诊断文件和数据库事件。收到extension_send却无对应extension_reply表示停在扩展/Chrome交互范围，仍需扩展端日志才能进一步区分二者。

实时画面按需采集：有工作台观看者且任务running/needs_human或人工接管中才更新；任务结束、停止或出错且无人接管时停止画面采集，保留最后一帧。主动截图按钮仍可使用。

评测中心：访问 `/evals`（工作台侧栏入口），只读聚合evals目录中的cases.jsonl及results目录下的results.json/jsonl、验证JSON。展示用例、历史运行、对比指标、状态筛选和case已有执行链路，每15秒自动刷新。缺少内部日志明确标注，不补造根因。API复用工作台认证，使用索引ID访问产物，禁止任意路径和符号链接。
