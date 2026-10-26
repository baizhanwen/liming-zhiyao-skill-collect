# GitHub 来源、搜索与技能包

连接器优先使用 GitHub 官方 REST API，只读请求 `api.github.com`。匿名 API 遇到限流、网络或超时错误时，仓库分析可自动改走受限的公开 HTTPS Git 读取。应用设置中的 Token 只经后端发送；不会自动复制本机 `gh` 登录。任何读取方式均不执行仓库代码、README 指令或安装脚本。

## 使用

1. 工作台粘贴 `https://github.com/owner/repo`，或 `tree` 目录、`blob` 文件链接，选择快速/深入分析。
2. 应用解析默认分支、明确分支/标签或提交；支持分支名含 `/` 的链接，通过最长有效 ref 解析。随后所有文件均固定到 40 位提交 SHA。
3. 来源快照包括 README、许可证、依赖和部分文档/源文件，保存文件路径、Git blob SHA、SHA-256、固定版本链接与正文。README 属于项目自述，读取代码属于代码观察，均不自动获得运行验证。
4. “发现 Skill”先作真实仓库搜索；有 Token 时另调用官方代码搜索。Token 缺失时明确提示代码搜索不可用，仍可进入仓库发现多个 `SKILL.md`。
5. 发现结果逐项展示技能路径、正文、文件清单、来源许可证及“未执行”状态。选择具体技能后导入完整范围。Stars 和更新时间仅作信息，不代表质量或安全审核。

自然语言整理属于模型层；连接器对收到的搜索词执行关键词搜索，并附加 `skill`。结果不保证穷尽 GitHub；GitHub 的搜索索引、权限与限流均可能影响结果。认证代码搜索失败时保留真实仓库搜索结果和错误原因。

## 授权和限流

在设置中填写具备所需仓库读取权限的 GitHub Token；不要粘贴到聊天或 README。公开仓库通常无需 Token；私有仓库必须授予读取权限。官方代码搜索需要认证。HTTP 401、403、404、429、5xx、网络和超时分别返回结构化错误；上游响应正文不进入错误日志，以免意外回显敏感信息。

客户端不自动读取全局 `gh` 登录或系统凭证。开发验收可在独立进程中明确使用已有 `gh` 登录，凭证只在进程内存，不能因此宣称新安装应用已配置 Token。

### 匿名 API 配额耗尽时

仓库链接分析会尝试匿名公开 Git 降级：在临时 bare 仓库中浅层获取一个提交，只获取小于 512 KiB 的 blob；不创建工作区、不 checkout。随后从 Git 对象读取已选择的说明、许可证、依赖和关键源文件，逐个校验 Git blob SHA，证据仍链接到固定提交。

来源元信息明确记录 `transport: public_git`、原 API 错误码 `fallback_reason`、实际磁盘用量及安全预算。Stars、仓库更新时间、SPDX 标识在 API 不可用时保持未知，不编造元数据；真实许可证正文仍可保留为文件证据。API 成功时记录 `transport: github_api`。

该降级仅适用于未配置 Token 的公开仓库快照；认证失败、权限拒绝、私有仓库/404、配置了 Token 的请求均不绕过原错误。搜索、多技能发现和完整 GitHub 技能包仍使用 API，其配额耗尽时可在工作台粘贴仓库链接先分析，或自行配置 GitHub Token。Git 不存在时也给出明确说明。原失败任务保留，修正连接条件后在任务详情“重试”即可继续同一任务。

Git 子进程使用清理后的环境：禁用全局/系统 Git 配置、credential helper、交互密码、hooks、LFS、子模块、外部和本地文件协议、HTTP 重定向及懒获取；仅允许由已验证 `github.com/owner/repo` 构造的 HTTPS remote。不会接受任意 remote 或 `file://`。

Git 的底层 libcurl 可能读取 netrc，所以降级额外清空 Authorization，设置空 netrc 路径；检测到默认目录存在 `.netrc`/`_netrc` 时直接拒绝该降级（不读取内容），避免旧版 libcurl 使用本机凭证。此时可在应用设置中自行提供 API Token。

每次降级共享 75 秒时限；监控临时目录，达到 128 MiB 或 50,000 个文件即结束进程树；单进程标准输出、错误输出也有上限。监控按短间隔检查，磁盘预算不是操作系统硬配额。服务端不支持过滤、仓库过大或网络不通时会明确失败，不会退回无界完整 clone。临时资料结束后删除；源文件不会写入用户已有仓库。

## 安全与边界

- 输入只接受 HTTPS 的 `github.com` 仓库、目录或文件链接；拒绝用户名密码、非标准端口、其他主机和不安全路径。API 请求目标固定，下载 URL 和重定向不被跟随。
- Git 树最多保留 30,000 项，响应最大 8 MiB；GitHub 自身截断或本地限量会明确标记。包导入遇到不完整树直接拒绝，不声称完整。
- 快速快照最多 8 个文件、60,000 字符；深入最多 18 个文件、150,000 字符。单文件 512 KiB。快照是有界静态阅读，不是完整代码审计。
- 仓库发现最多加载 40 个技能正文，并返回总发现数和截断状态。同仓库技能不自动合并。
- 单个技能包最多 100 个文件、8 MiB，保留所选技能目录的真实文件及仓库许可证/声明。符号链接、子模块、越界路径、哈希不符及超限均拒绝。
- 每个 blob 按 Git 对象 SHA 验证。ZIP 的 `manifest.json` 保存来源、提交、文件清单、SHA-256 与内容哈希；来源原有 manifest 另存为 `references/original-manifest.json`，避免覆盖 provenance。
- 根目录 `SKILL.md` 会把整个仓库视为技能范围；过大的根技能包应由维护人提供更小的完整 ZIP。目录外引用文件不会被自动扩散下载；使用前检查正文依赖。
- 许可证未知表示未确立复用授权，用户应审阅后决定。导入不会安装到当前 AI 的全局技能目录，也不会执行任何脚本。

## 复查

```sh
.venv/bin/python -m pytest tests/test_github.py -q
```

该测试使用显式 `httpx.MockTransport`，覆盖固定提交、斜杠分支、目标范围、多技能发现、无 Token 降级、认证代码搜索、完整包/许可保留、符号链接和子模块、Git 树截断、哈希及脱敏错误。真实外部记录单列在 `artifacts/verification/github-live.json`，不把契约测试写成真实 GitHub 联通。

公开 Git 读取另有真实本地 Git 对象契约测试（传输明确模拟，不允许生产 `file://`），验证斜杠分支、文件/目录范围、固定提交、超大 blob/符号链接跳过、配置与凭证隔离、输出/磁盘/超时停止。实际无 Token 的 Easel 读取单列在 `artifacts/verification/github-public-git-live.json`：API 实际限流后，经公开 Git 成功读取，不能据此声称模型分析或运行 Easel 已通过。

官方依据：

- [搜索 API 和认证限制](https://docs.github.com/en/rest/search/search)
- [Git trees API](https://docs.github.com/en/rest/git/trees)
- [仓库内容 API 和大小/目录限制](https://docs.github.com/en/rest/repos/contents)
- [Git blobs API](https://docs.github.com/en/rest/git/blobs)
- [Git fetch 过滤与浅层读取](https://git-scm.com/docs/git-fetch)
- [Git 配置和协议白名单](https://git-scm.com/docs/git-config)
- [Git ls-tree 对象列表](https://git-scm.com/docs/git-ls-tree)
- [libcurl netrc 行为](https://curl.se/libcurl/c/CURLOPT_NETRC.html)
