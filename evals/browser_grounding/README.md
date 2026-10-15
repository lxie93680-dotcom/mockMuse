# 浏览器定位工具对照评测

评测对象是当前 Muse 的真实 Chrome 扩展 + `muse.browser.Browser.perform`，与真实 Playwright MCP。两者使用同一 Chrome for Testing 可执行文件、1280×800 视口、隔离临时配置，在本机受控网页上执行。没有替换生产 Muse 工具，也不操作个人 Chrome。

18个场景，每个3个合成变体，每个变体运行两个方案，共108次。包含普通按钮、同名候选、隐藏候选、延迟加载、DOM替换、旧ref重新分配、遮挡、禁用、屏幕外、超过120个控件、同源/跨域iframe、Shadow DOM、悬停菜单、新标签、找不到目标、无法消歧和三步路书草稿。

这是工具层评测：统一的脚本控制器从各自工具返回的可见文字、上下文、角色和引用中选择目标，不使用模型，不调用CSS选择器或DOM脚本操作被测页面，不启用截图回退。它衡量工具在这些场景的可观察性、动作可靠性和旧引用误点风险，不能作为完整Muse Agent或真实高德任务成功率。

## 文件

- `cases.jsonl`：任务、场景、验收和变体。
- `fixtures.py`：本机合成页面及浏览器事件上报。
- `protocol.json`：协议、公平性边界、限制。
- `run.py`：真实浏览器启动、两个适配器、统一控制器、独立判分和报告。
- `launch_muse.mjs`：加载现有扩展的真实隔离浏览器。
- `runtime/package-lock.json`：固定依赖；MCP 0.0.83。
- `results/comparison-v1/`：正式结果，逐次工具入参、返回、耗时与源文件SHA-256。

执行器只从工具输出选择目标。判分器单独从受控站点事件确定输入、点击、候选选择和保存是否真实发生；工具声称成功不算通过。任务完成与安全停止分开统计，误点击单列。源extension只复制到临时目录，并将默认8000端口改为评测服务器随机端口；浏览器控制代码不变。

## 复现

```bash
npm --prefix evals/browser_grounding/runtime ci
.venv/bin/python evals/browser_grounding/run.py \
  --repeats 3 --name comparison-new \
  --chromium '/absolute/path/to/Chrome-for-Testing'
```

只跑部分场景：`--cases unique,ref_reuse_safety,cross_origin_frame`。已有结果目录不会覆盖。需要本机Chrome for Testing支持加载解压扩展。默认自动查找本机已安装的chromium-1243，仅为当前环境便利；其他机器请显式传`--chromium`。

每个方案共享自己的隔离浏览器上下文，逐场景生成独立页面URL和判分状态；没有跨用例复用引用。种子改变候选编号/顺序、加载延迟和干扰控件数量。全部是开发场景，不声称保留集泛化能力。耗时不包含浏览器冷启动；API Token和费用未测量。`model_visible_chars`只是工具文本字符数，不是Token或费用。

`smoke/`、`calibration/`、`policy-check/`用于校准评测器，不纳入正式结论；其中曾发现匹配器错误，保留原始调试证据。正式运行前修正了这些错误。

工具层通过后，下一阶段应固定同一模型、提示、预算和页面变体，比较完整Agent端到端表现，加入视觉地图控件和真实高德只读流程。受控数据成功率不能直接替代真实网站的验收。
