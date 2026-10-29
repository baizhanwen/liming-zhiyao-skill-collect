# 第三方来源与许可

本项目应用代码使用 MIT 许可。运行时依赖按各自许可分发；安装器从锁文件安装，不在源码仓库附带完整依赖副本。

| 依赖/来源 | 许可/用途 |
|---|---|
| React / React DOM | MIT，前端 UI |
| Vite / React Vite plugin | MIT，前端构建 |
| TypeScript 7.0.2 | Apache-2.0，类型检查；以锁定安装包的 LICENSE 为准 |
| Playwright / Playwright Test | Apache-2.0，项目匿名浏览器采集运行时及界面验收；锁定在前端依赖中 |
| Playwright 配套 Chromium / Chrome for Testing | 安装器从官方分发下载，具体条款及组件许可以浏览器附带 LICENSE/NOTICE 为准；本仓库不捆绑浏览器二进制 |
| FastAPI / Starlette / Pydantic / Uvicorn | MIT/BSD，后端与校验，准确条款以安装包 LICENSE 为准 |
| HTTPX | BSD-3-Clause，HTTP 连接 |
| cryptography | Apache-2.0/BSD，Fernet 本机凭证加密 |
| python-multipart | Apache-2.0，上传处理 |
| pytest / pytest-timeout | MIT，开发验收 |
| FFmpeg | 外部系统程序，其许可受用户安装构建选项影响；本仓库不捆绑其二进制 |
| [Easel](https://github.com/ZJU-REAL/Easel) / [展示](https://zjureal.com/Easel/) | 仅作为视觉参考和真实来源读取验收对象；未复用其代码、标志或素材 |
| [anthropics/skills](https://github.com/anthropics/skills) | 真实多技能发现与包完整性验收对象。公开验收只存路径、SHA与统计，不重新分发技能正文/脚本；应用导入包保留来源许可 |

GitHub 技能的仓库级许可不一定覆盖每个技能。导入前查看真实 SKILL.md、相关文件和各级 LICENSE/NOTICE。应用完整包会保存范围内文件和上级许可声明；许可 UNKNOWN 不代表获得使用授权。导入不授予执行验证状态，也不会执行脚本。

界面图标为本项目手写 SVG 路径，未复制 Easel 资产。合成媒体契约夹具使用本机 FFmpeg 色块/正弦波，仅证明对应测试流程，未冒充用户视频或真实模型理解。真实原片验收单列脱敏统计与哈希，原媒体及私人材料不随仓库分发。
