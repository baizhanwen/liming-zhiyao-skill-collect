# 可视化工作台验收

本组验证使用真实 Chrome 浏览器、已构建 React 界面和真实 FastAPI/SQLite 本地服务。测试服务自动在 `127.0.0.1:8766` 启动，数据写入系统临时目录，不读取或修改日常工作空间的凭证和资料。模型和飞书均未配置私人凭证，不把此测试记作外部联通成功。

## 复现

完成项目安装及前端构建后运行：

```bash
npm run test:ui --prefix frontend
```

默认调用本机 Google Chrome。没有 Chrome 时，可安装 Playwright 的 Chromium 并指定使用它：

```bash
cd frontend
npx playwright install chromium
PW_CHANNEL=chromium npm run test:ui
```

Windows PowerShell 的 Chromium 指定方式是 `$env:PW_CHANNEL="chromium"`，再执行 `npm run test:ui`。本次实际执行平台为 macOS，其他系统的 UI 测试命令未实测。

## 验证内容与边界

- 无配置启动、空提交禁用、模型设置入口；文字、视觉、转写独立呈现。
- 设置页保存空配置；不发起未配置的模型请求。
- 手写的 `[验收样例] source-review` 技能通过真实 API 创建、编辑生成 v2、读取版本，正确展示解码后的文件正文。
- 完整 ZIP 导出后上传导入，正文保留且本地相同技能不增加副本；附件与二进制包完整性的多文件验证由后端测试另行覆盖。
- 关键词与标签筛选；知识创建、修改和归档；技能归档与归档回看。
- 自编 SRT 上传后进入待配置状态，保留字幕内容与时间证据；实际取消任务。未声称完成文字模型分析、转写或画面理解。
- 飞书未配置读取产生可见错误，未展示伪造企业记录。
- 真实搜索页面入口及认证降级说明；GitHub 外部真实搜索由连接器实测记录另验。
- 桌面 `1440 × 1100` 与窄屏 `390 × 844`；知识和技能编辑、设置、导航与错误提示可操作，检查水平宽度无溢出。
- 全流程没有浏览器未捕获异常。

测试产生的样例知识与技能已归档；所有测试数据均在独立临时目录内。

## 证据

机器可读结果：`artifacts/verification/ui-browser.json`。Playwright 完整结果：`artifacts/verification/playwright-report.json`。

截图使用真实本地服务页面；技能截图清楚标注为手写验收样例：

- `artifacts/verification/workbench-desktop.png`
- `artifacts/verification/workbench-mobile.png`
- `artifacts/verification/settings-desktop.png`
- `artifacts/verification/settings-mobile.png`
- `artifacts/verification/skill-editor-desktop.png`
- `artifacts/verification/skill-editor-mobile.png`
- `artifacts/verification/subtitle-pending-mobile.png`
- `artifacts/verification/discovery-empty-desktop.png`

截图前关闭临时提示、滚动到顶部并结束界面过渡动画，避免将动画中间帧当作响应式布局证据。

## 飞书正文与完整包的一致性提示

补充的 UI 契约回归显式模拟飞书 API 响应，验证普通导入遇到企业正文与附件不一致时只更新个人副本，并显示“完整附件仍待刷新”。同步预览的 `refresh_package` 会说明确认后重新打包并更新原企业记录；点击明确确认之前不会触发同步写入。保留企业版本的 `resolved_remote` 结果根据 `package_refreshed` 说明是否刷新了企业附件。

这组前端契约只验证提示和确认流程，不代表飞书真实读写通过；连接器真实读写和同步一致性另按接入与验收记录核对。可用 `UI_ARTIFACT_DIR=/tmp/skill-collect-ui-check npm run test:ui --prefix frontend` 将回归输出写入独立目录，保留既有公开截图和报告。

## 模型输出与本机诊断

文字和图片模型卡的“高级设置”支持 `128—6000` 整数输出上限，默认 `3000`；转写接口不展示无效的输出上限选项。Base URL 的主机名精确为 `api.deepseek.com` 时，显示关闭推理（默认）、启用推理和沿用供应商默认三个选项。设置保存不会自动发起模型调用。

失败任务提供“查看本机模型诊断”，点击后才读取该任务的本机诊断接口。每次回答记录、校验原因和原始回答分别折叠，原始内容不默认显示，不提供公开导出操作。诊断只用于解释截断、格式或证据校验失败，不代表模型结果已经验收通过。

对应的前端契约回归使用显式测试设置和失败回答样例，验证主机名判断、整数校验、设置保存、诊断按需请求和原始回答展开。不会读取用户密钥，也不会发起真实模型请求；外部 DeepSeek 实测由项目的独立真实联通记录说明。

## 2026-10-29 项目关联与自动主路径的定向界面契约

本轮新增桌面 `1440 × 1100`、窄屏 `390 × 844` 两项项目关联契约，最新运行 **2 项通过**。真实 Chrome 操作已构建界面，但任务、模型与 GitHub 响应均为显式模拟；“打开 GitHub”的新窗口也由本地契约拦截，没有访问真实仓库。数据、截图和模型地址均为自行编写的样例，不包含用户原片、私人配置或凭证。

本轮验证范围：

- 明确链接只标仓库存在已核验，名称检索的唯一或多个结果仍为候选；歧义、成功零结果、无项目线索、旧任务未关联、限流和失效来源快照分开显示。
- 引文回查任务来源，展示实际音频段范围或画面取样点；未知引用不提升为证据。用户输入与来源链接明确标注“非原片证据”，平台 AI 摘要保持文本范围。
- 文本、字幕、转写及 `frames_only` 的完成范围与标题独立呈现；静音视频仅标“仅抽样画面（无音频转写）”。
- 查看引用可跳转至来源证据；手动打开仓库由契约拦截，点击分析才向既有任务 API 创建一次指定仓库任务，并显示文字模型目标地址。
- 单独重新检索只调用项目关联 API，没有模型调用。主页说明由工作台自身自动获取原片、转写、分析画面和关联项目，无需 Codex；缺 Chromium 的获取失败显示阶段与原因，保持未完成，补传明确作为备用降级。
- 两种视口下上述按钮可操作，无横向溢出，未出现浏览器未捕获异常。

最新结构化报告为 [ui-project-links-contract-20261029.json](../artifacts/verification/ui-project-links-contract-20261029.json)。逐视口检查记录分别为 [桌面](../artifacts/verification/ui-project-links-desktop-contract-checks.json)、[390px](../artifacts/verification/ui-project-links-mobile-contract-checks.json)。全部从已执行的临时结果脱敏整理，本次整理没有重跑测试。

较早另有两项受影响回归通过，单独记录在 [ui-project-links-regression-contract-20261029.json](../artifacts/verification/ui-project-links-regression-contract-20261029.json)：一项使用临时目录的真实本地 CRUD、ZIP 与字幕待配置流程，一项显式模型诊断契约。该轮早于最新自动主路径文案和 `frames_only` 调整，不把它当作后续源码的完整复验。

历史完整 5 项没有在本轮重新运行；新增 2 项使当前定义共 7 项，但没有据上述分次运行声称最新全套 7 项已经通过。历史截图与原报告保持不变。**本节不证明指定抖音 URL 自动获取原片、真实转写／视觉／文本分析或真实 GitHub 关联通过；这些业务仍需独立真实证据。**

新增公开截图均带 `contract` 文件名，画面内明确标为契约样例。项目关联图为对应视口中的区域截图，自动获取受限图为整页截图：

- [桌面项目关联](../artifacts/verification/ui-project-links-desktop-contract.png)
- [390px 项目关联](../artifacts/verification/ui-project-links-mobile-contract.png)
- [桌面自动获取受限](../artifacts/verification/ui-project-acquisition-desktop-contract.png)
- [390px 自动获取受限](../artifacts/verification/ui-project-acquisition-mobile-contract.png)

## 2026-10-29 首页最近资料的归档过滤复验

本次仅调整 `Home` 的最近技能与最近知识：先过滤 `archived`，再取前三条，空态也使用过滤后的集合。共享列表继续保留归档数据，个人技能和知识库的归档入口不变。没有删除、恢复或修改任何实际资料。

前端 `0.2.0` 的 TypeScript 检查及 Vite 生产构建通过。随后使用独立的全新 Chrome 会话，只读访问正在运行的真实本地服务，实际加载了本次新构建；没有操作原有浏览器会话、提交任务或调用模型。

实际已有数据的复验结果：

- 有效技能 1 条、归档技能 4 条：首页只显示 1 条有效技能，归档卡片显示数为 0；勾选个人技能“已归档”后，原 4 条记录仍全部可见。
- 有效知识 4 条、归档知识 0 条：首页显示有效知识中的前三条；知识归档视图对应 0 条。未为复验制造归档知识或全归档数据，相关数据边界未另行运行。
- 桌面与 `390px` 首页可正常展示，窄屏页面宽度为 `390px`，无横向溢出。没有浏览器未捕获异常；所有浏览器请求均为 GET，前后资料与归档标记保持一致。

公开脱敏结果见 [ui-home-archive-readonly-20261029.json](../artifacts/verification/ui-home-archive-readonly-20261029.json)，只含计数、检查范围与构建标识，不含实际卡片名称或私人配置。真实页面截图及详细观察仅保存在 Git 忽略的私有目录，本次没有把真实资料截图公开。

本次没有新增镜像测试，也没有重跑完整 UI 或后端测试套件；它是首页与归档入口的真实本地只读复验，不是模型、飞书、抖音媒体或 GitHub 外部业务的重新验收。服务未停止或重启。
