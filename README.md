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

## 操作系统适配与部署

Muse 根据宿主操作系统选择命令、进程管理和浏览器路径。`muse status` 和 `/api/state` 的 `environment` 返回当前系统、shell 与能力限制。

| 能力 | macOS | Linux | 原生 Windows |
| --- | --- | --- | --- |
| Web、CLI、远程 HTTP MCP、工具目录与搜索 | 支持 | 支持 | 已实现适配，待真实 Windows 验收 |
| Chrome 扩展与 Playwright MCP | 支持 | 需图形桌面或无头浏览器 | 已实现路径、环境和 Node 启动适配，待真实 Windows 验收 |
| 命令执行适配层 | zsh、进程组 | bash、进程组 | PowerShell、UTF-8、Job Object 进程树管理 |
| Agent 文件读取和文本补丁 | 暂存＋原子目录交换 | 暂存＋原子目录交换 | Windows 10+：受控读取/补丁、冲突检查、备份和逐项写回；待实机验收 |
| Agent 本地命令与 Filesystem MCP | 使用现有沙箱 | 需 bubblewrap | 命令沙箱未实现，继续禁用 |

服务自动识别 Windows/macOS/Linux；Windows 额外读取 major/minor/build，Windows 10/11 选择日志写回实现，旧版本不开放本地文件事务。Windows 文件事务仅支持本机固定 NTFS 卷，开始任务前检查实际卷能力；macOS/Linux 保留原子目录交换实现。`muse status` 的 `environment.publication_mode` 展示选中的策略。

Windows Agent 使用 `read_file` 分页读取暂存文本、`apply_patch` 修改暂存文本；原文件只由宿主最终写回。Agent 不会收到终端工具，外部 Filesystem 旧目录项也会被搜索、描述和调用过滤。低层 PowerShell 适配器不是 OS 写入沙箱，不提供 Agent 执行授权。远程 MCP 自身的能力仍由其服务端及现有启停/授权策略决定。

Windows 写回流程：创建暂存副本 → 完成任务 → 检查原目录基线 → 冻结最终版本 → 锁定目录和受影响文件并重新校验 → 原文件移入备份 → 不覆盖已有目标地放入新文件 → 持久化提交回执。若其他编辑器抢先创建了目标，写回失败，保留用户文件；不自动合并。目录删除会在移动前后检查空目录，发现外部新增内容则尝试恢复。

异常时按日志反向恢复；恢复同时校验内容和文件标识，遇到外部修改则保留现场并标记 `recovery_blocked`，不会强行覆盖。服务启动及下一次任务启动时检查未完成事务；恢复不重跑模型任务。备份与日志位于原目录旁的 `.<原目录名>-muse-win-transactions/<任务ID>/`，成功后保留 `backup/` 和 `transaction.json`，恢复失败时也保留 `files/`、`publication/` 等证据。没有自动清理备份或人工冲突解决界面。

该策略保留原路径，但不是整目录原子提交：写回期间可能短暂出现新旧文件混合或单个目标缺失。日志包含文件数据刷盘与原子替换，进程中断恢复已通过模拟故障验证；尚未验证 Windows 实机和真实断电恢复，不承诺断电持久性。第一版拒绝重解析点（含 junction）、硬链接、NTFS 命名数据流、仅大小写重命名、文件/目录类型互换和已有目录权限更改；不能保留权限或无法获取独占句柄时停止。

### 原生 Windows

需要 Python 3.11+、uv、Node.js 22+，浏览器模式还需 Chrome 和相应扩展。在项目目录：

1. 运行 `install-Muse.cmd` 安装 Python 与两个内置 Node MCP 的依赖；已有 `.env` 不会覆盖。
2. 编辑 `.env` 配置模型。默认浏览器使用扩展连接；按本文扩展说明安装、授权。
3. 双击 `start-Muse.cmd`，保持终端开启。已运行时只打开工作台；代码或配置变更后使用 `restart-Muse.cmd`。

PowerShell 也可直接执行：

```powershell
# 可选：同时安装专用浏览器（仅 dedicated 模式需要）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\muse-windows.ps1 -Action install -InstallBrowser
# 自定义端口；自动同步 MUSE_PORT 到浏览器连接器
.\start-Muse.cmd -Port 8001
.\restart-Muse.cmd -Port 8001
.\.venv\Scripts\muse.exe status
```

重启脚本先核对监听进程是否使用本项目虚拟环境 Python 和 `uvicorn muse.app:app`；其他程序占用端口时拒绝终止。Windows 重启会中断正在执行的任务，不提供任务自动续跑；需要保留执行结果时先等待任务完成。脚本不修改全局 PowerShell 执行策略。

Node 不在 PATH 时可配置 `MUSE_NODE_EXECUTABLE=C:/Program Files/nodejs/node.exe`。专用浏览器自动查找 `%LOCALAPPDATA%/ms-playwright`，也支持 `PLAYWRIGHT_BROWSERS_PATH` 或 `MUSE_PLAYWRIGHT_EXECUTABLE` 显式路径（不包含包裹引号）。专用模式需支持扩展加载的 Playwright Chromium，不自动替换成普通 Chrome。扩展模式可使用 `MUSE_PLAYWRIGHT_CHROME_EXECUTABLE` 指定 Chrome。

### Linux / 云电脑

Linux 可执行 `bash start-Muse.sh`；macOS 保留原 `启动Muse.command`。Windows 本地文件工具受上述系统版本、卷类型与沙箱限制；云电脑仍需目标环境验收。

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

跨平台核心验证与可选的真实 Chromium/MCP 测试：

```sh
uv run python -m unittest tests.test_host_compatibility tests.test_muse_cli tests.test_remote_mcp tests.test_tool_access -v
# macOS/Linux：真实浏览器测试，需先安装 vendor/playwright 依赖与 Chromium
MUSE_RUN_BROWSER_SMOKE=1 uv run python -m unittest tests.test_host_compatibility.RealBrowserCompatibility -v
```

PowerShell 使用 `$env:MUSE_RUN_BROWSER_SMOKE='1'` 后运行相同 unittest 命令。`uv run python -m unittest tests.test_windows_workspace -v` 验证 Windows 写回协议，其中故障注入使用测试 IO 模型，Win32 独占句柄与实际写回测试仅在 Windows 执行。`.github/workflows/host-compatibility.yml` 包含三平台兼容性、Windows 文件事务和真实无头浏览器测试；添加 CI 不等于已执行成功。真实 Chrome 扩展附着、画面与人工接管仍需在目标系统手动验收。

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

独立 Linux 执行服务保留用于部署；当前 Agent 不注册 Linux 文件或终端工具，相关旧工具定义已移除。

在**云端 Linux**安装 Docker 与 Compose 后，在项目目录执行：

```sh
# 生成独立执行端口令，不要与工作台的 1234 口令共用
export MUSE_LINUX_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
docker compose -f deploy/compose.linux.yml up -d --build
```

将同一口令配置到云端 Muse `.env` 的 `MUSE_LINUX_TOKEN`，并设置 `MUSE_LINUX_URL=http://127.0.0.1:8765`，然后启动或重启 Muse。Compose 运行时也需要这枚口令；将其保存在仅部署用户可读的环境文件中，后续使用 `docker compose --env-file 文件路径 -f deploy/compose.linux.yml up -d`。不要将口令提交到代码仓库。

文件持久化在 Docker 的 `linux-workspace` 命名卷内，对应容器 `/workspace`，没有挂载本地 Mac 目录、Docker socket、模型密钥或云主机个人目录。终端以非 root 运行，容器根目录只读，限制内存、进程、CPU；工作目录与 /tmp 可写。Bash 可以访问容器内其他可读文件并使用网络，文件相对路径检查不是 shell 沙箱，容器才是隔离边界。不要在该容器放凭据。终端超时和退出后清理同进程组子进程，不用于后台常驻服务。文本读写和输出上限256 KiB；不提供独立递归删除工具。

端口仅绑定云主机回环地址。跨机器使用 HTTPS 或 SSH 端口转发，不能直接公开该终端服务。执行端在非 Linux 系统拒绝启动。文件测试用临时目录和模拟平台，不执行本地终端；真实 Bash、容器资源限制、持久化和云端 Chrome 图形会话需部署后验收。

## 本地工具

旧 `local_files` / `local_terminal` 入口及实现已删除。命令执行、文本补丁修改和图片查看使用下述原生内置工具；目录读写、搜索也可使用 Filesystem 外部 MCP。正式目标目录默认为 `data/workspace/`，可通过 `MUSE_LOCAL_WORKSPACE` 指定。Agent 实际在任务级隔离副本内修改，成功后由宿主一次提交。

- `browser_crop_image(x,y,width,height,scale)`：按 browser_view 原图像素坐标裁剪当前截图并发送聊天，scale为1–4。必须先看截图确认位置；放大不会增加细节。

正式 Agent 任务的文件写入仅允许目标目录的暂存副本；原有 `MUSE_LOCAL_ALLOWED_FILES` 不能扩大任务写入范围。要修改工作区外文件，需先将其纳入正式目标目录，而不是逐文件绕过暂存。

## Filesystem 本地 MCP

Filesystem作为外部stdio MCP随Muse启动/退出连接和清理；命令执行、文本补丁及图片查看使用Muse原生内置工具。Desktop Commander已移除。

```sh
npm ci --prefix vendor/filesystem --ignore-scripts
# .env：MUSE_FILESYSTEM_MCP=1
```

Filesystem版本固定1.0.0，Node.js需要18+。启动通过tools/list获取定义并缓存，模型通过tool_search/tool_describe/tool_call按需访问Filesystem工具。允许目录为`MUSE_LOCAL_WORKSPACE`（默认data/workspace），逐文件授权不会自动扩大为Filesystem目录权限；HOME位于data/mcp-home，子进程不继承模型密钥。启动失败显示错误，原生内置工具仍可使用。

资料：[Filesystem MCP](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem)。

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

## 工具目录存储（第一阶段）

`muse/tool_store.py` 提供独立 `ToolStore(path)` 存储层，可复用 `data/muse.db`。已接入启动和任务组装时的定义登记；按需搜索和调用见第五阶段，管理页面见第七阶段。

- `tool_catalog`：工具稳定ID、来源、来源内稳定key、名称、描述、参数schema、启停及可用状态。元数据不保存连接、执行函数或凭据。
- `tool_fingerprints`：对规范化的名称、描述、参数schema计算版本化SHA-256。对象键排序，数组顺序保留；启停与健康状态不参与哈希。工具ID独立于内容指纹。
- `tool_catalog_fts`：独立持久化FTS5 trigram索引，支持名称、描述、参数和来源的字面关键词查询；不足3字符的查询使用子串匹配，支持中文。它不是语义搜索；实际权限与连接状态由后续注册层校验。
- 注册批次事务更新工具、指纹和索引；内容未变只更新检查时间。刷新定义保留启停/可用状态，初始可用状态为unknown。`upsert_many`不是完整发现快照，列表缺失不表示删除。
- `rebuild_index()`从工具表原子重建派生索引。启停缓存、通知/文件监听及移除对账见后续阶段说明；故障分类见第六阶段，产品工具页面见第七阶段。

验证：`.venv/bin/python -m unittest tests.test_tool_store`。

## 原生内置命令、补丁和图片工具

`muse/native_tools.py`直接提供四个工具，MCP启停不影响注册，不属于Filesystem：

- `exec_command(command, workdir='.', yield_time_ms=1000, timeout_seconds=60)`：macOS使用zsh，Linux使用Bash；先等待1–10000ms，命令仍运行则返回当前任务专属session_id。最长运行1–300秒。正式 Agent 任务由 harness 创建隔离暂存区，macOS 使用 Seatbelt 限制写入，Linux 使用 bubblewrap（未安装时拒绝命令，不回退）；采用精简环境，不继承模型密钥或加载用户 shell 启动配置。
- `write_stdin(session_id, chars='', yield_time_ms=1000, terminate=False)`：继续读取输出或写入管道输入，可终止进程组。不是PTY，不支持依赖真实终端的程序。stdout/stderr各保留最近64 KiB，每次各返回最多6000字节，溢出明确标记；session_id为空表示已退出且输出已读完。任务完成、失败、停止或人工交还时清理命令进程组，不用于常驻后台服务。
- `apply_patch(patch)`：支持`*** Begin Patch`/`*** End Patch`包围的Add File、Update File、Delete File。Update段以独立`@@`开头，使用唯一精确原文上下文；支持`*** End of File`。不支持行号、模糊匹配或Move to。每个文件和补丁上限2 MiB，UTF-8文本。补丁仅修改当前暂存副本；每次补丁不会同步正式目标。任务成功后，由宿主提交器统一发布完整最终版本。
- `view_image(path)`：将本地PNG/JPEG/WebP/GIF图片作为真实视觉输入返回给模型；最多20 MiB/2500万像素，首帧缩略至最长边2048。此图不是浏览器截图，不能据其坐标点击网页。

文件/图片路径和命令起始目录绑定任务暂存区，正式目标内的绝对路径由原生工具映射到暂存副本；任务中不接受工作区外的逐文件写授权。路径工具拒绝隐藏文件和符号链接。终端仅可写暂存文件与任务 scratch；macOS 另禁止硬链接、通用网络和应用控制，仅放行配置的本机 Muse API 端口以保留 CLI 管理链路。Linux bubblewrap 使用独立网络命名空间，暂不提供本机 CLI 网络桥接。子Agent禁止命令执行、会话输入和补丁修改，可继承只读图片查看。普通任务不因可选MCP/浏览器未就绪而预先拒绝，实际使用相应工具时才报告故障；演示模式仍检查浏览器连接。

验证：`.venv/bin/python -m unittest tests.test_native_tools`；不调用模型API。

## Muse Browser MCP（统一浏览器工具）

所有模型可调用的浏览器能力统一由`muse/browser_mcp.py`提供，Agent内置列表不再重复注册浏览器/截图函数：

- `browser_action`：网页打开/读取/点击/悬停/填写/选择/按键/滚动/等待及标签管理。
- `browser_view`、`browser_click`：截图观察与坐标点击。
- `browser_send_image`、`browser_crop_image`：当前页面截图发送、区域裁剪后发送到Muse聊天。
- `read_browser`：专供只读场景的读取入口；子Agent可用此工具及browser_view，不能调用混合读写的browser_action或截图发送/裁剪。

底层SDK兼容工具名带`mcp_Muse_Browser__`前缀；正常Muse任务通过第五阶段三个统一入口按需访问，不把这些函数逐项提供给模型。这是独立的MCP工具来源，不计为Muse内置工具；它使用MCP 2.x的真实client/server及进程内内存流，不新增HTTP端口或stdio子进程，目前不能作为一个独立进程接给其他客户端。共享宿主的浏览器、实时画面和聊天图片发布函数；底层Playwright MCP继续由浏览器生命周期管理，不重复向模型暴露原生工具。旧扩展后端也走同一工具门面。

应用生命周期连接/关闭Browser MCP，单独调用run_agent时可自动创建并清理任务级连接。验证码/登录引发的人工交还保持原控制异常；跨MCP的调用保留任务诊断上下文及取消传播。request_human仍是Muse内置工具，实时画面与人工接管UI由宿主提供。工具目录已通过启动/任务组装登记到持久化存储；按需搜索见第五阶段；管理页见第七阶段。

验证：`.venv/bin/python -m unittest tests.test_browser_mcp tests.test_demo tests.test_local_tools`。MCP协议及模拟浏览器/模型回环测试不等于真实网站端到端验收。


## 工具定义登记

`muse/tool_registration.py`仅将外部工具的真实名称、描述和完整参数schema写入工具表，事务生成/更新SHA-256指纹与搜索索引。Filesystem和浏览器分别为`mcp:filesystem`、`mcp:muse-browser`，均标记为外部MCP，共14+6=20项。9个内置工具完全由代码注册，不存储定义、指纹或索引。旧库中的builtin记录会在打开ToolStore时联动清理；存储接口拒绝新注册builtin来源。已移除的local_documents与Desktop Commander不登记。

应用启动和任务组装时通过MCP tools/list登记外部定义，无需模型密钥或模型请求；不再为目录登记构建内置工具或模型客户端。相同定义保留ID/指纹、只更新检查时间，定义刷新不覆盖用户启停状态。失败来源保留已有记录并报告失败，不以空列表替代。来源未连接不等于工具已删除；仅成功完整发现快照中的缺失项按第三阶段标记missing，不硬删除历史记录。availability仍保持独立健康字段，不因有定义就认定后台浏览器可操作。

注册持久化已接入；定义变化、重连刷新和移除对账见下方第三阶段。启停状态的执行拦截与7天缓存已接入（第四阶段）；tool_search见第五阶段；工具管理页面见第七阶段。

## 统一动态注册接口（第二阶段）

`muse/tool_registry.py`提供`ToolRegistry.register(provider)`和`register_all(providers)`，统一接收`ToolProvider.discover_tools()`返回的`ToolDefinition(key,name,description,parameters)`。内置工具不经过注册中心，也不写工具表。每个来源先完整发现与校验，再事务写入定义、指纹、索引；来源之间独立提交，失败来源保留原有数据。同一来源的并发注册串行处理，变更定义不覆盖用户启停选择；稳定key可在展示名称变化时保留tool_id。重复来源、重复key/名称、无效schema、超限定义均拒绝。源对象、连接、HTTP URL/headers等运行时信息不写入工具表；注册错误日志不输出可能含凭据的异常原文。

`muse/tool_providers.py`包含三类适配器：

- `MCPProvider(server)`：从真实MCP `tools/list`获取定义，Filesystem和Muse Browser的来源ID保持不变。
- `ManifestProvider('plugin:example', path)`：读取JSON文件的`tools`数组，各项包含name、description和inputSchema（或parameters），可选key用于稳定身份；最多2 MiB，不导入或执行插件代码。
- `HTTPToolsProvider('http:example', url, headers=None)`：GET获取同样的工具清单或OpenAPI 3 JSON。支持基本路径/查询/请求头/cookie参数和application/json请求体、非递归本地引用；不支持外部引用、循环引用、multipart等。认证headers仅保留运行时，不跟随重定向。请求超时可配置至30秒，响应最多2 MiB。只获取定义，不发送业务请求或执行工具。

```python
from muse.tool_registry import ToolRegistry
from muse.tool_providers import ManifestProvider, HTTPToolsProvider

registry = ToolRegistry('data/muse.db')
await registry.register(ManifestProvider('plugin:example', 'plugins/example/tools.json'))
# 注册配置中明确指定的HTTP定义来源：
# await registry.register(HTTPToolsProvider('http:example', schema_url))
```

应用启动创建共享注册中心`muse.app.tool_registry`，启动与任务组装的MCP登记均走该接口。其他来源可通过同一异步接口动态登记；已支持MUSE_TOOL_SOURCES_CONFIG启动配置加载，目前没有来源安装配置UI，也不自动重建插件/HTTP业务连接。成功完整发现后按第三阶段规则对账缺失工具，支持通知/文件监听和连接恢复后的定义刷新；不自动恢复故障连接，也不把新插件/HTTP定义自动暴露给模型执行。内置工具仍由代码注册；正常任务的MCP按需访问方式见第五阶段。

验证：`.venv/bin/python -m unittest tests.test_tool_registry tests.test_tool_registration`。


## 工具定义刷新（第三阶段）

- MCP：Muse拥有的Filesystem/Browser连接在连接成功及收到`notifications/tools/list_changed`时发出事件。注册中心合并短时间内事件，在后台失效SDK列表缓存并重新发现；不在通知回调内同步tools/list。刷新中再次收到通知会继续刷新，不丢失变化。不支持该通知的服务可手动刷新；没有实现自动重启故障MCP进程。
- 插件文件：对已登记JSON定义文件每秒检查一次内容SHA-256，不监控业务文件。支持原子文件替换、删除/权限故障后恢复、无效JSON修正后恢复。读取/校验失败保留旧目录；不会把文件删除或解析失败当成空工具列表。首次发现失败的已配置来源也可在修复文件后自动恢复。
- 重启：现有两套MCP在启动时重新发现。可通过`MUSE_TOOL_SOURCES_CONFIG`指向外部来源JSON配置，重启后重新加载并发现；插件path相对配置文件所在目录，HTTP认证headers只从headers_env指定的环境变量读取。新增/删除来源配置需重启或通过注册接口处理，本阶段不热加载来源配置文件本身。
- 对账：成功完整快照中消失的工具保留目录记录、ID、指纹和启停选择，availability置为missing并从FTS索引移除；重现时恢复为unknown并重建索引，不自动启用。定义、指纹和索引更新在同一事务完成，失败整批回滚。重建索引同样排除missing项。
- 手动刷新：经既有鉴权的`POST /api/tools/refresh`，JSON为`{}`刷新全部已配置/登记来源，或`{"source_id":"mcp:filesystem"}`刷新指定来源。未登记返回404，单来源失败返回503且保留旧数据。不会执行或重试任何业务工具调用；第七阶段页面已提供重新发现按钮。
- 生命周期：注册中心退出时退订通知、取消文件监听和排队刷新。第五阶段接入后，模型持有稳定的三个入口，后续搜索/描述/调用可读取最新目录；不需要动态更换模型函数列表。

来源配置示例（没有配置则仍只登记现有两套MCP）：

```json
{"sources":[{"id":"plugin:example","kind":"plugin","path":"manifests/example.json"}]}
```

验证：`.venv/bin/python -m unittest tests.test_tool_refresh`。实际源未变化时指纹保持不变，不会为刷新强制重写索引。


## 启停写穿缓存（第四阶段）

`muse/tool_enable_cache.py`管理外部工具的启停缓存，内置工具不入库且不在开关范围内。第一次读取时从DB完整加载，固定TTL为604800秒（7天），使用单调时钟；读取不续期。过期后下一次读取查询DB并重建，进程重启冷加载。启停操作先提交DB，再同步修改缓存对应条目，不清空缓存、不延长其他条目的TTL；DB写失败时不修改缓存。注册/移除/重现时将同事务内读取的来源状态合并到缓存，不延长完整快照TTL。对DB的外部直接修改可能等到过期或来源同步才反映，管理操作应使用注册中心接口。

- `GET /api/tools/enabled`：经现有鉴权返回外部工具ID→布尔状态及缓存剩余秒数。
- `POST /api/tools/{tool_id}/enabled`：JSON `{"enabled":false}`或true。严格布尔输入；未知ID返回404。该接口已接入第七阶段工具管理页面。
- 第五阶段改为三个统一入口，外部工具搜索先按启停缓存与委派范围过滤，再限制条数；内置工具保持代码注册。
- 每次真正调用MCP前再次检查启停及missing状态；父Agent和已复制的子Agent工具对象同样生效。子Agent工具清单也读取最新启停状态，原有只读权限限制保留。
- 已经开始的调用不会因开关被强制中断，也不撤销已发生的修改。第五阶段启用后，进行中的任务再次搜索即可发现工具，无需动态新增函数schema。开关限制的是特定外部工具，不是宿主OS文件访问沙箱。

验证：`.venv/bin/python -m unittest tests.test_tool_enable_cache`。7天过期通过注入时钟模拟，不是实际等待7天；无真实模型API调用。


## 按需工具披露（第五阶段）

主Agent模型仅接收内置工具和`tool_search`、`tool_describe`、`tool_call`三个统一入口。外部工具定义保存在目录中，不逐项加入初始模型工具列表。`tool_search(query, limit)`返回精简候选（ID、来源、名称、简介、是否具有执行器），不会返回完整schema或执行工具；查询先按启用/禁用缓存和本次授权范围过滤，再应用LIMIT。`tool_describe(tool_id)`返回当前完整JSON Schema及SHA256指纹。`tool_call(tool_id, arguments, fingerprint)`校验当前权限、指纹、参数和连接，随后执行；不符合schema、未知ID、已禁用、missing或过期指纹均不执行。参数验证支持本地schema引用，但不加载远程引用。

MCP调用保留SDK的来源过滤、工具注解、审批策略以及图像返回；调用时检查来源当前定义与目录一致。要求审批的调用交还人工，配置专用工具guardrails的来源当前拒绝走通用执行路径，避免绕过SDK限制。JSON插件和HTTP/OpenAPI当前仅支持定义发现，没有实现业务执行器，描述返回`executable=false`，调用会明确报错。失败分类与重新发现见第六阶段，不自动重试业务调用。

子Agent获得独立的三个入口，后端按原有只读排除列表裁剪外部工具，不能继承主Agent的通用调用权限。`list_subagent_tools`列出可委派工具的精简信息；选择外部工具时把`tool_id`作为`delegate_task.tools`条目，选择内置工具时使用其名称。省略tools继承允许范围，[]不提供工具，显式选择只能缩小范围。子Agent自身的搜索、描述、执行均检查该范围和启停缓存。

共享应用注册中心支持任务运行期间新增、移除、启用或禁用工具；下次搜索/描述/调用读取最新目录和缓存，不重新拼接全部函数schema。单独调用run_agent且没有上下文存储时使用任务级临时目录，任务结束清理，不污染产品数据库。浏览器页面投影、原文归档、截图图片、人工交还和取消继续走原有处理。默认应用任务有9个内置工具及3个统一入口，实际数量取决于是否配置历史存储。

验证包含临时数据库、内存MCP和本地HTTP模拟模型的搜索→描述→调用完整SDK循环，覆盖三种模型配置；未使用真实模型API或实测线上token/延迟收益。代码加载需要重启现有Muse服务。


## 调用失败分类与重新发现（第六阶段）

`muse/tool_failures.py`按异常类型及SDK异常因果链分类，不解析工具返回的任意文字来触发恢复。连接中断、超时、MCP method-not-found、当前来源定义与目录不一致或无过滤策略下工具消失，会触发受限的来源重新发现。普通业务错误（包括MCP isError）、参数错误、权限拒绝、人工交还与用户取消不会触发恢复；未知执行异常按执行错误处理，不假定为连接问题。

重新发现复用注册中心来源适配器：MCP失效列表缓存后重新tools/list，插件清单重新读取，HTTP/OpenAPI重新GET定义；按完整成功快照对账，更新定义、指纹、索引和启停缓存中的来源条目，保留用户开关。没有调用业务方法、启动/重启外部进程或自动重放原业务动作。同来源并发失败合并为一个任务，自动发现每来源至少间隔5秒，单次预算5秒；失败保留旧目录，正常通知/文件监听/手动刷新不受自动恢复节流限制。

模型收到结构化失败字段`category`、`stage`、`rediscovery`、`call_may_have_executed`和`business_call_retried=false`。执行阶段失败保守标记可能已执行，先核实结果；执行前失败明确尚未发出业务调用。`rediscovery=refreshed`仅代表目录更新成功，不能当作业务成功。错误输出不包含原异常中的地址、凭据或参数；人工交还和任务取消保持原控制流。

恢复函数与来源类型无关；目前插件/HTTP仍无业务执行器，因此没有为其虚构业务重试能力。原生内置工具继续沿用原有退出码、超时和错误处理，不进入外部目录恢复。自动重新发现不承诺重建已断开的传输；确实断连且tools/list仍失败时明确返回failed，需来源宿主恢复连接或重启服务。验证使用临时DB、内存MCP、模拟异常和短预算时钟，无真实线上断网或自动进程重启测量。


## 工具管理页面（第七阶段）

主工作台左侧“工具”入口打开 /tools，展示所有持久化外部工具；内置工具不入库也不出现在该页面。页面显示工具数量、启用数量、来源数量，支持按名称/简介/来源搜索、按来源及启停状态筛选。已禁用或missing的工具仍保留在管理列表中，方便查看和重新配置；Agent工具搜索继续按启停缓存及授权范围过滤，二者用途不同。

列表与详情通过现有鉴权读取：GET /api/tools 返回精简工具和来源列表，GET /api/tools/{tool_id}/detail 返回定义、参数schema与指纹。启停状态从同一启停缓存读取，避免页面使用DB旧状态而Agent使用缓存。POST启停接口提交DB并直接更新缓存，页面成功后同步状态与计数；保存失败保留原展示并报错。已有调用不会被撤销。

“更新列表”仅重读管理数据；“重新发现”刷新选定来源或全部来源，沿用目录对账逻辑，不执行业务工具。部分来源失败时显示错误且保留旧定义。工具状态“未验证”不代表可用性已确认，“有定义”也不等于连接可用。详情允许查看禁用和已移除的定义，但不绕过Agent执行权限；工具内容以textContent展示，不执行来源提供的HTML。API沿用登录cookie、本机访问与跨来源写入校验。

页面包含登录、空列表、无搜索结果、加载期间操作锁定、错误和部分刷新失败提示；支持窄屏布局，宽表在内部横向滚动。验证包括4项临时DB接口测试，以及ego-browser在独立临时Muse实例中的搜索、筛选、禁用、详情、重新发现后设置保留、重新启用、空结果和桌面/390px窄屏视觉检查。没有修改现有产品DB或实际外部工具状态；原运行服务需重启加载新增路由。


## 整体验收（第八阶段，2026-10-16）

本轮工具生命周期改造的本地验收已完成，范围与证据如下。这里的通过不等于真实模型、真实网站或生产故障恢复测试。

| 项目 | 实现与验证 |
| --- | --- |
| 外部工具存储 | 稳定ID、版本化SHA256、独立FTS索引，事务更新；内置工具不入库 |
| 动态注册 | MCP、JSON插件清单、HTTP/OpenAPI定义来源统一登记；失败保留旧快照 |
| 刷新对账 | 通知、文件内容变化、重连、重启和手动刷新；缺失/恢复保留ID及开关 |
| 启停缓存 | 固定7天、读不续期、开关写穿；搜索在LIMIT前过滤，调用再次校验 |
| 按需披露 | 主Agent仅接收内置工具和三个统一入口；子Agent使用独立受限范围 |
| 失败处理 | 类型分类、有界合并重新发现、不重放业务调用、明确可能已执行 |
| 管理页面 | 所有外部工具、筛选、详情、启停和重新发现；前阶段已做桌面/移动端浏览器验收 |
| 连接替换边界 | 新来源发现失败或被取消时保留旧连接调用拦截；新来源成功后旧连接工具对象拒绝调用，关闭时解除包装 |

验收发现并修复了来源替换期间提前移除旧连接启停拦截的问题。通知订阅与业务调用权限绑定分别维护；同一运行时连接重复登记不累积包装，退役连接在注册中心生命周期内仍受拦截。新增5项跨阶段验收：失败替换、成功替换、发现中/取消、持久化重启后的搜索与指纹/索引/范围联动，以及已安装Filesystem MCP的真实stdio只读回环。后者只读取临时目录，验证20项目录（Filesystem14 + Browser6）以及禁用后的搜索/调用拦截；未安装Node或vendor依赖时该项会明确跳过。

当前环境全量201项测试通过，无跳过。依赖锁检查、JS语法及diff空白检查通过。实际产品数据库以只读方式执行SQLite完整性检查，重算全部20项SHA256并核对FTS成员及重复项，结果通过；20项启停状态保持启用。一次初始手工只读回环因临时目录未解析macOS的/var符号链接被Filesystem拒绝，改为解析后的授权路径后通过；没有放宽工具路径限制。

复现命令：MUSE_BROWSER_BACKEND=extension .venv/bin/python -m unittest discover -s tests。测试包含本地HTTP模拟模型和内存浏览器MCP；不调用真实模型API，不声称线上token、时延或准确率提升。JSON插件/HTTP目前仅支持定义发现，专用SDK guardrails工具拒绝通用执行，故障MCP不自动重启，来源配置文件本身不热加载；工具管理页需手动更新列表。现有运行服务尚未重启，需重启才能加载本轮代码。


## 重启后的实际实例复验（2026-10-16）

本次发现8000端口仍由10月15日启动的旧进程占用，旧运行态包含Desktop Commander且新工具接口返回404。启动脚本检测到已运行服务时只打开页面，并不重启；已明确脚本文案，并正常停止旧进程，按原.env加载新实例。新实例确认20项外部工具、Filesystem14 + Browser6，全部新管理路由可用，启动登记无错误；实际管理页面搜索、来源筛选、schema与指纹详情通过，手动完整重新发现返回变更/移除/恢复均0且无错误。

独立验收会话中使用真实DeepSeek配置完成tool_search→tool_describe→tool_call→Filesystem list_allowed_directories，运行态事件记录三个入口均completed，原文归档标记实际来源；测试后切回原会话，保留验收会话供核对。真实浏览器读取暴露扩展尚未附着和MCP请求超时，本次没有把浏览器端到端操作判为通过。发现SDK的REQUEST_TIMEOUT和CONNECTION_CLOSED以MCPError错误码返回，分类器此前漏识别；已按SDK常量补齐，不靠异常字符串猜测，新增协议超时恢复回归测试。全量202项测试通过。浏览器实际连接与页面操作的复验结果另见当日调优记录。

实际实例复验最终结果：文件MCP真实模型链路通过；浏览器read_browser和browser_view在扩展未附着时均超时，故浏览器正常读取/截图尚未通过。修复后两次实际失败均记录category=timeout、rediscovery=refreshed、business_call_retried=false，未自动重放。管理接口真实Host鉴权、写入Origin校验、未知工具404检查通过；验收后恢复原会话，20项开关保持启用。当前服务已加载本次修复，不再是旧进程。浏览器需要先完成扩展连接，再验收正常路径。

## 浏览器连接恢复完成（2026-10-16）

此前“浏览器未通过”结论已由本次实测更新：按原有官方扩展流程接回普通Chrome后，运行态browser_connected=true、connection_state=connected。真实DeepSeek模型在本地control-test页面通过三个统一入口完成read→fill→click→read→browser_view；DOM和实际JPEG截图均显示测试输入成功，工具完成事件无错误。用户原会话已恢复，业务网站未被修改。

同时修复首次连接可能携带迟到业务动作的边界：扩展尚未附着时先用browser_tabs/list建立只读握手，成功后才派发导航、输入或截图请求。握手超时/取消时不派发原动作；重复使用已连接会话不额外握手。新增3项顺序、超时和取消测试，全量205项通过。代码已加载并完成真实验证。重启后官方扩展仍可能要求重新确认连接；本改动不绕过其授权流程。


## 工具搜索与生命周期评测集（2026-10-16）

evals/tool_lifecycle/cases.jsonl 定义7个用例，覆盖6类需求：工具删除前后搜索、禁用后直接搜索与对话查询、自然语言触发写文件和浏览器、热缓存更新后获取最新描述、新参数/指纹变更后重新描述执行、真实临时进程重启重新加载插件和HTTP来源。模型结论不作为唯一评分依据，检查实际搜索输出、描述原文、指纹、执行效果、权限缓存及进程重启后的持久化结果。同一对话保留历史，验证工具变化后模型不会仅依赖旧schema。

搜索实现仍是SQLite FTS5 trigram，名称/描述/参数/来源字面检索，多关键词AND，BM25排序；短词走子串匹配。没有embedding、向量数据库或向量相似度。自然语言发现依赖模型把任务转成搜索关键词，并非底层语义检索。

运行命令：

    .venv/bin/python -m evals.tool_lifecycle.run
    .venv/bin/python -m evals.tool_lifecycle.run --live --provider deepseek
    .venv/bin/python -m evals.tool_lifecycle.run --live --provider deepseek --cases disable_visibility schema_reload

默认离线不调用模型；自然语言两项标为invalid/requires-live，不伪称通过。--live才使用当前配置的真实模型；每次创建新的results目录，不覆盖历史。run.json记录代码/用例/依赖锁哈希和不含凭据的模型信息，results.jsonl保存每项步骤和失败，summary.json汇总。评测中心动态扫描新用例和运行产物。

每个用例使用独立临时DB、MCP来源和文件，finally恢复函数定义与开关并核对；新工具按真实产品语义保留missing墓碑，临时目录最终整体删除。真实重启案例启动/停止临时Muse进程，停止后修改插件和HTTP清单，再启动检查新内容、稳定ID及禁用状态，最后恢复原来源并再次启动确认。异常也会关闭子进程、HTTP服务并删除临时命名空间。不会改生产工具、配置、数据库、Chrome或当前对话。故障注入测试覆盖修改后失败仍恢复、模型口头“保存”不能代替文件效果，以及重复运行。

自然语言写文件使用已安装Filesystem MCP的原始英文定义和临时授权目录，不靠中文合成工具凑通过；浏览器案例使用真实内存MCP协议和合成浏览器，只测工具选择及调用，不等同真实Chrome页面操作验收。写文件主评分检查真实工具触发、实际文件存在且含目标文字；逐字精确度另列指标，额外换行/字符不会被隐藏或声称精确通过。

已执行多轮并保留失败结果：live-2暴露普通文本返回被误按JSON解码，以及“搜索探针”提示被理解为网页搜索；修正评测器并明确生命周期用例查询工具目录。live-3核心6/7，写文件已触发但字节比较失败；按照本次“能否触发工具”目标拆分工具触发与逐字精确度，保留差异。最新live-4核心7/7，真实模型36次请求；文件精确度0/1，尾部额外字符仍明确记录。此结果不是全面语义检索能力或内容生成完全正确的证明。

本次全量212项测试通过。离线评测在回归测试中连续运行两轮，清理均通过。生产20项工具定义、参数、ID、可用状态和启停与评测前快照逐项一致，浏览器保持连接；未为新增评测重启当前服务。

## 工具评测扩充至54项（2026-10-16）

工具生命周期评测现为54项、13类；上节7项是扩充前的历史结果。

| 类别 | 项数 | 主要变体 |
| --- | ---: | --- |
| 删除与恢复 | 4 | 删除、空快照、禁用后恢复、多次删除恢复 |
| 启停过滤 | 4 | 对话与直接搜索、全部禁用、LIMIT前过滤、反复切换 |
| 自然语言触发 | 8 | 写文件中英文/Unicode/不同措辞；浏览器中英文/标题/只读 |
| 缓存 | 4 | 更新、固定TTL到期、DB写失败、未变化注册 |
| 定义变化与通知 | 7 | 参数增删、描述变化、通知合并、刷新中通知、取消 |
| 重启 | 4 | 内容更新、未变化、空定义、稳定键重命名 |
| 搜索边界 | 7 | 中文短词、短英文、引号、多词AND、无匹配、数量限制、大小写 |
| 来源隔离 | 3 | 同名工具跨来源、替换失败、替换成功后旧连接拒绝调用 |
| 指纹 | 3 | 对象键顺序、启停独立、数组顺序变化 |
| 故障恢复 | 4 | 传输错误、业务错误、并发恢复合并、发现失败保留快照 |
| 子Agent权限 | 3 | 禁止工具、显式范围/空范围、搜索后禁用 |
| 存储与索引 | 2 | 索引重建、内置工具拒绝入库 |
| 参数校验 | 1 | 缺必填、类型错误、多余字段，不产生执行效果 |

支持按类别选择和重复执行，例如：

    .venv/bin/python -m evals.tool_lifecycle.run --category 7 --repeats 2
    .venv/bin/python -m evals.tool_lifecycle.run --live --provider deepseek --category 3 --repeats 2

expanded-offline-2：46项通过，8项自然语言用例明确标为需真实模型；expanded-live-1：54项核心检查通过，其中19项经过真实对话，共89次模型请求。4项写文件均真实触发并包含目标文字，逐字精确度另计为2通过、2不通过，具体差异保留在结果。浏览器仍为合成浏览器，不代表真实Chrome端到端结果；故障与生命周期变更也为隔离测试场景。

全量214项回归通过。评测结束全部临时命名空间删除；生产20项工具的定义、参数、ID、可用状态和启停与评测前快照一致，浏览器保持连接。评测中心已显示54项及本轮结果。每次运行使用新结果目录并独立恢复，不覆盖历史；测试同时验证重复运行、分类筛选和未知用例不能静默通过。

## 评测中心区分能力评测与系统测试（2026-10-16）

评测中心现在以“能力评测”“系统测试”分别展示集合、统计及运行记录，不合并两类通过率。原tool_lifecycle的54项及全部历史运行归入系统测试（包括模型辅助的生命周期场景），原目录/历史产物保留。protocol.json的suite_type决定分类；未标注的旧集合默认能力评测，旧compression设计回归归系统测试。当前已启动的旧API也可由页面按已知集合兼容分类，加载新页面即可查看，无需为了分类重启浏览器连接。

新增tool_search能力评测：固定生产20项工具定义快照，加8项合成干扰工具；40条人工二元标注查询，8类各5项：工具名、关键词、自然语言、同义表达、跨语言、干扰项、多相关工具、无匹配。直接使用真实ToolAccess.search及启停缓存路径，原始查询不经模型改写，不执行业务工具；快照不会静默随生产变化。标注是当前版本的相关工具集合，并非完整真实用户分布。

    .venv/bin/python -m evals.tool_search.run

每次新目录保存查询、相关标注、实际候选和排名、各项分数、分类汇总及源码哈希。正向35条分别计算Hit@1/3/5、Recall@5、MRR@5、Precision@5；无匹配5条单算正确返回空的比例，不混进正向指标。Precision分母为实际返回候选数（最多5）。逐条pass仅表示Top5命中至少一个相关工具，不能代替完整召回指标或任务完成率。

baseline-1实测：Hit@1=42.9%，Hit@5=51.4%，Recall@5=49.0%，MRR@5=46.7%，Precision@5=44.6%；5条无匹配查询全部正确返回空。自然语言、同义表达、跨语言三组Hit@5均为0，保留失败，不调整标签去凑分。它测量原始查询的检索器效果，不等于实际Agent经过关键词改写后的发现效果；后者需另设独立实验。本轮未修改检索算法，也不声称线上质量提升。

## 本人标注搜索相关性（2026-10-16）

入口：评测中心 → 能力评测 → 工具搜索效果 → 本人标注。逐条阅读查询，在全部28项工具（含干扰项）中多选相关工具，或明确勾选“此工具库中没有合适工具”；可查看工具描述和参数、筛选工具、填写备注、保存草稿或确认并进入下一条。支持按待标注/草稿/已确认筛选及中途退出后继续。默认不勾选工具，原始自动标注折叠显示供参考，不能算本人确认。

人工标注独立存于evals/tool_search/annotations.sqlite3（本地文件，Git忽略），每次保存追加版本；不会覆盖cases.jsonl中的自动标注、原始用例或历史成绩。保存需要当前查询/工具快照哈希及上一版本号，陈旧页面返回409而不覆盖；数据集变化后旧标签仍保留在历史表，但不自动作为当前版本已确认标签使用。数据沿用Muse访问鉴权和跨来源写入限制。

使用本人已确认标注重跑：

    .venv/bin/python -m evals.tool_search.run --labels human
    .venv/bin/python -m evals.tool_search.run --labels human --allow-partial

第一条要求40条全部确认；第二条仅计入已确认部分并标记partial，草稿不参与。默认不带--labels仍为generated自动标签，页面会明确显示“自动生成标注（未经本人确认）”。人工运行会固化labels.json及标注哈希，后续改标不会改变旧成绩。当前功能不在网页里启动评测，不新增模型调用；标注为相关/不相关二元标签，不包含分级相关度或多人审核。所有具有当前Muse写权限的访问者都可标注，不能用来证明登录者的独立身份。

## 运行任务初评与人工复核（2026-10-16）

与参考答案标注不同，复核入口在“评测中心→选择一次运行→点击任务表格行”。任务表显示任务、运行方式、原始状态、初评分、人工复核分数、耗时和调用数；支持未复核/已复核/人工改分/初评待评筛选。评测集用例改为表格；任务详情用分数卡、预期与实际候选排名表、评分理由、执行步骤及复核表单展示，原始JSON仅作折叠排查资料。汇总与配置也提供格式化字段表。

初评为可复现的规则初评，不是另一个模型的主观裁判：搜索正向查询用Recall@5×100并单列首个相关排名、MRR、Precision和未召回项；负向查询按是否正确返回空记100/0。生命周期仅按系统断言给100/0；浏览器沿用任务判定并检查误点；压缩沿用严格正确判定。运行异常、无效样本、未知规则或不足证据标待评，不能算能力零分。搜索参考标签尚未经本人审核的局限仍保留，初评分也继承此局限。两类任务分数和搜索正负样本不合成一个总分。

可以认可初评、改为0–100分或标记证据不足；改分/证据不足要求理由。保存使用乐观锁，所有复核追加历史；不改原始产物、参考标签或初评分，不重跑任务、不调用模型。初评和复核存于本地evals/run-reviews.sqlite3（Git忽略），绑定运行文件内容SHA256、行号和规则版本；原始文件变化后旧复核保留但不套用新结果。新运行在评测中心读取时生成并固化初评。规则代码改动须升级规则版本，避免把新规则冒充旧分数。

本次为已有382条运行任务生成初评：363条有分、19条待评、0条正式人工复核。无逐条结果的基础设施记录不强行生成分数。全量230项测试通过；隔离页面验证了表格展示、初评分与改分并列、改分后关闭重开持久化、人工改分筛选及40条用例表格。没有在正式数据上冒充用户复核。所有有Muse写权限的访问者均可复核，没有新增多用户审核身份体系。

### 远程 MCP 设置

从会话侧栏进入「MCP 设置」（`/settings/mcp`），填写唯一服务名称和 HTTPS MCP URL，点击「添加并连接」。首版支持 Streamable HTTP、无鉴权或 Bearer Token；需要 Token 时，页面填写服务端环境变量名，实际值在 `.env` 中配置后重启 Muse。暂不支持 OAuth、SSE、带查询参数的 URL，以及页面编辑/删除服务。

配置保存在数据目录的 `remote-mcp.json`，仅保存地址和 Token 环境变量名。启动时自动恢复连接；连接失败会保留配置，可点击重试。成功完成工具发现和目录注册后才显示已连接，工具定义存入现有 `muse.db` 工具目录，通过已有搜索、描述、调用入口使用，无需独立 MCP 网关。运行中连接/超时故障会触发会话清理和重建，每 30 秒通过工具目录刷新检查空闲连接；失败后按 30 秒至 10 分钟指数退避重试。页面每 5 秒更新连接状态，手动重试会立即重建会话。目录和工具启停设置保留，失败的业务调用不会自动重放。

### 对话接入远程 MCP（内置 Skill）

Muse 内置 `install-mcp` Skill。用户提出接入请求后，主 Agent 可先 `skill_read("install-mcp")`，再通过 `manage_mcp` 的 `list` / `add` / `connect` 操作复用设置页的连接管理器。完整指引按需读取，不全量注入提示词。该管理能力不下放给只读子 Agent。

例如：「帮我接入这个远程 MCP，名称 travel，地址 https://服务商域名/mcp，Token 环境变量 TRAVEL_MCP_TOKEN。」地址必须由用户提供或在服务商资料中核实；仅有服务名称时不能猜 URL。成功注册后，工具可在同一任务中通过 `tool_search`、`tool_describe`、`tool_call` 使用。此 Skill 不安装本地软件或 stdio 服务，不读取凭据，不支持 OAuth。返回 `saved` 表示配置保存，`ready` 表示连接和工具注册完成，两者需区分。

## 浏览器工作区按实际使用展开（2026-10-16）

主界面默认聊天布局，右侧浏览器不预先展开。只有Muse Browser MCP实际执行浏览器工具（含读取、截图、点击、截图发送/裁剪）时才通知界面展开；工具目录发现、搜索或描述不会触发。手动点击顶部“打开浏览器”、演示及人工接管也可显式启用。收起后新的浏览器调用可以重新展开。浏览器未连接时显示等待扩展确认及错误提示，不把MCP进程已就绪误写为Chrome已连接。

普通任务不启动截图流；有浏览器使用且任务运行中、等待人工时才采集，人工接管也可保持采集。任务结束后停止持续采集，并保留最后实际画面；成功任务完成前补一帧，截图失败单独提示而不抹掉业务结果。聊天布局中执行记录移至聊天区的折叠面板，浏览器布局中放回右侧。

每次新任务清空浏览器使用标记、旧画面和地址；新会话默认关闭，历史会话保存其实际浏览器使用标记，切换时仍清空旧画面。帧带会话/任务ID，界面忽略不匹配的迟到帧；未启用工作区时后端也丢弃迟到帧。没有关闭浏览器/MCP或改动工具定义，只按需展示和采集。

全量249项测试通过；新增发现不触发、普通任务/陈旧接管不采集、最后一帧仅在实际使用时获取、迟到帧过滤及会话恢复检查。ego-browser在隔离合成浏览器上通过真实内存MCP验证普通任务→浏览器调用→普通任务的布局/画面变化并检查截图；不等于本次重新验收真实Chrome扩展连接。正式服务已加载；当前官方扩展未附着时，首次实际使用仍需按提示完成连接确认，不绕过其授权步骤。

## Muse CLI与内置使用Skill（2026-10-16）

当前管理路径为“读取muse-usage Skill→exec_command执行muse CLI→当前服务API→共用连接管理器/工具注册中心”。专用manage_mcp及其安装Skill运行时读取门禁已移除；通用skill_read保留。旧install-mcp名称兼容读取新版说明，但不再有单独的安装管理工具。上一节会话Skill读取状态表保留历史数据，当前不再依赖它阻止CLI执行。

唯一使用说明位于[muse-usage Skill](muse/skills/muse-usage/SKILL.md)，CLI的muse docs和muse skills read读取同一文件。Agent启动只注入简短Skill名称/描述及入口，按需读取全文；不把完整CLI文档重复塞入每轮规则。

项目已增加可安装console入口。项目目录执行uv sync --locked --inexact后，可用uv run muse，或激活.venv后用muse；Agent命令环境已将当前Python环境bin加入PATH，任意授权工作目录均可调用，不需要暴露服务商密钥。也支持python -m muse.cli。

    uv run muse --help
    uv run muse docs
    uv run muse status
    uv run muse mcp list
    uv run muse mcp add microsoft-learn --url https://learn.microsoft.com/api/mcp
    uv run muse mcp connect microsoft-learn
    uv run muse tools search microsoft_docs --limit 5
    uv run muse tools list
    uv run muse tools describe TOOL_ID
    uv run muse tools disable TOOL_ID
    uv run muse tools enable TOOL_ID
    uv run muse tools refresh

CLI不直接写remote-mcp.json，不创建离线的第二套注册器；服务不在线时明确失败。add由现有服务原子保存配置，再建立连接并登记工具；同一运行服务和当前Agent共用目录立即可见。新/api/tools/search复用ToolAccess和启停缓存，先过滤再LIMIT；管理list仍包含禁用/缺失工具，二者用途不同。业务执行继续使用现有三个工具入口及其权限/指纹校验。

结果默认JSON，help/Skill为文本；0代表操作正常完成，1代表API/连接等错误，2可表示保存但未连接或刷新存在错误（也用于命令语法错误）。必须检查saved/ready，不把配置保存或后台重连当作业务成功。凭据只用服务端环境变量名；CLI不读取.env，访问口令从CLI自身进程环境变量读取，本机沿用现有鉴权。默认127.0.0.1:8000，可用MUSE_URL/MUSE_PORT或--server覆盖；不向非本机HTTP地址发送请求，不跟随重定向。


## 任务文件暂存与最终提交

每次正式 Agent 任务由宿主创建 `<正式目录的父目录>/.<目录名>-muse-transactions/<任务标识>/`：`files/` 是独立可编辑副本，`scratch/` 用于进程 HOME/TMPDIR，`transaction.json` 和 `task.lock` 是 Agent 不可写的宿主事务控制。正式目标在终端沙箱中只读；Filesystem MCP 按任务启动，同样仅获准暂存目录并受系统沙箱限制，任务结束前关闭。补丁、命令及文件 MCP 不逐次写回正式目录。

任务成功并停止命令/文件服务后，宿主计算最终内容、检查正式目标基线，将暂存结果复制到 Agent 不可写的 `publication/` 并校验内容。这避免持有暂存文件句柄的残留进程修改发布内容。最终只调用一次 macOS `renamex_np(RENAME_SWAP)` 或 Linux `renameat2(RENAME_EXCHANGE)`，将完整 publication 目录与正式目录原子交换；不支持原子交换或跨文件系统时拒绝提交，不降级为逐文件覆盖。无变更任务不交换目录。原子可见性限于目录路径切换；已打开的外部文件句柄仍指向旧 inode。

提交以持久化 commit_id 和 prepared/committed 回执防止同一任务重复交换；启动新任务时核查 prepared 事务，识别“交换完成但回执未落盘”和“尚未交换”，不盲目重放。新旧版本无法匹配时阻止新任务并要求处理。发生外部修改冲突时保留暂存且拒绝覆盖；基线检测为乐观校验，外部写入者仍应在发布期间暂停写入，或遵循同一宿主锁。

失败/取消不提交，人工暂停保留同一任务的暂存副本供恢复；异常退出留下的 staged 事务也可在取得任务锁后按原任务恢复；暂停期间正式目录变化则拒绝恢复。成功后清理工作副本和旧版本，保留提交回执；失败与冲突副本保留用于恢复，目前没有自动清理策略。暂不支持工作区外独立文件的合并提交，源目录中硬链接/特殊文件会拒绝暂存，新增或修改的符号链接拒绝发布。目录复制和最终快照会增加磁盘与启动开销，未测量性能。

验证：`.venv/bin/python -m unittest tests.test_staged_workspace`。覆盖真实 macOS 终端隔离、实际 Filesystem MCP 暂存写入、整批交换、冲突、暂停、提交回执恢复、重复提交与旧句柄隔离；不调用真实模型 API。


暂存回归补充（2026-10-16）：事务校验目标目录、commit_id 和持久化指纹版本，失去任务锁的旧对象不得提交或中止恢复后的任务。Agent 命令和补丁入口必须绑定当前活跃暂存事务，关闭后不可再次调用。初始复制中断可重建未完成副本；提交复制失败、磁盘写满、fsync 失败及内容变化均拒绝发布。版本 2 指纹包含所有者、权限、文件 flags、扩展属性和 macOS ACL；macOS 使用系统 ditto 保留元数据，历史版本 1 回执仍可读取。

补充测试覆盖：初始复制失败重试、staged 异常恢复、旧句柄失效、事务身份篡改、无 harness/跨任务/关闭后的工具调用、锁竞争、重命名/删除/二进制/空目录/执行权限、扩展属性与 ACL、外部元数据冲突、提交副本篡改、prepared 两种恢复分支和不确定状态阻断、硬链接/FIFO/特殊权限拒绝、沙箱缺失时拒绝启动。磁盘和崩溃故障使用注入模拟；终端、文件 MCP、扩展属性、ACL 与原子交换在当前 macOS 实际执行。Linux、真实断电恢复、海量文件性能仍未实测；自动清理和恢复管理界面尚未实现。


远程 MCP 卸载：`muse mcp remove SERVER_ID`（别名 `muse mcp uninstall SERVER_ID`），调用鉴权的 `DELETE /api/mcp/{server_id}`。原子移除配置后停止连接/重连，注销执行器，将工具目录标记 missing 并同步索引和缓存；历史工具 ID 和启停偏好保留。重复卸载幂等，其他服务与服务端环境变量不受影响。启动时补齐配置已删除但工具目录尚未清理的中断卸载。当前不提供撤回远端已开始业务操作的保证。


工具目录服务关联：数据库 schema v2 在 tool_catalog 持久化可空的 server_id。远程 MCP 的值严格使用 remote-mcp.json 的服务 key，注册/重连时写入；启动时先根据已有配置回填旧工具记录，即使远端连接失败也可查到关联。来源未知或非远程 MCP 为 null，不从展示名称猜配置。tool_search 候选、tool_describe、管理列表/详情及来源列表均返回 server_id；CLI 支持 `muse tools list --server-id travel` 和 `muse tools search hotel --server-id travel`，搜索在 LIMIT 前按服务及权限过滤。用返回值可直接定位 remote-mcp.json[server_id]；本次不增加业务调用失败后的自动补建或重放。卸载后 missing 历史记录仍保留 server_id。迁移不更改 tool_id、参数指纹、启停偏好或可用状态。


Skill发现与读取（2026-10-17）：skill_list统一扫描内置muse/skills和安装user/skills下的SKILL.md，返回skill_id/name/description/source。skill_read(name, path='SKILL.md')按需返回正文或包内UTF-8引用资料，最多256 KiB；builtin/name和installed/name区分同名来源，短名歧义报错；旧install-mcp兼容读取builtin/muse-usage。每次扫描实际目录，不依赖内存/DB目录，不执行脚本或自动连接MCP。CLI muse skills list/read支持两类及--path，muse docs仍只读内置说明。头部使用安全YAML解析，校验name/description和非空正文；拒绝链接、隐藏路径和越界读取。此轮仅实现list/read，不含安装/更新/启停/卸载管理或语义search；已经进入对话的旧文本不会因磁盘删除自动撤回。
