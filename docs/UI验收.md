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
