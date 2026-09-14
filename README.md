# star-pulse

本地生成「GitHub 星耀榜」：每日采快照，每周出周报（含周增 Star 排名、分类透视、防刷星标记）。

零第三方依赖，只用 Python 3.11+ 标准库。CI 里不需要 `pip install`。

---

## 为什么必须每天跑，而不是每周跑

这是整个项目最重要的一件事。

**GitHub 没有任何接口能告诉你「某个仓库本周新增了多少 Star」**：

| 你可能想用的 | 实际情况 |
|---|---|
| Trending 页面 | 无官方 API，只能解析 HTML；显示的是 "stars today"，`?since=weekly` 也不会变成周增量 |
| REST `/repos/{o}/{r}` | 只返回**当前总星数**，没有历史 |
| REST `/repos/{o}/{r}/stargazers` | 实测**需要认证**（匿名返回 401）；且只返回前 40,000 个 |
| GraphQL `stargazers` | 能拿到带时间戳的完整列表，但 30 万星的仓库要翻 3000 次请求，直接撞限流 |
| OSSInsight 等第三方 | 见下方「为什么不用第三方」 |

所以唯一可靠的路线是：**每天给候选池拍一张星数快照，用两个时间点相减**。

由此推出一条硬约束和一个设计决定：

- 🔒 **硬约束**：从开始跑的那天算起，**第 8 天才会有第一个真正的周增榜**。冷启动无法绕过。
- ✅ **设计决定**：报告里同时提供一个**不依赖历史**的「新项目榜」，所以第一天就有产出。

> 如果你坚持「每周只跑一次」，周增量在数学上仍然成立（本周快照 − 上周快照），
> 但**任何一次运行失败或延迟，就会得到 14 天的增量**，和别人的 7 天混在一起排名。
> 本项目因此默认每日快照 + 每周出报告，并用 `max_span_days` 强制拦截超跨度的仓库。
> 真要每周只跑一次，把 `.github/workflows/daily.yml` 的 cron 改成 `0 20 * * 1` 即可。

---

## 为什么不用第三方数据源（实测结论）

OSSInsight 的公开 API 曾被广泛推荐用于回填历史星数。**本项目实测后放弃了它**：

```
GET https://api.ossinsight.io/v1/repos/ayghri/i-have-adhd/stargazers/history
→ 2026-09-01 的星数返回 19,297，而真实值是 43,467（低报约 55%）
```

它自己也在响应里标注了原因：

```json
"data_quality": {
  "status": "degraded",
  "source": "github_public_events_firehose",
  "severely_degraded_since": "2026-05-01",
  "note": "GitHub position-partitioned the /events firehose (~2025-05-23) ...
           star counts are lower bounds, not exact value"
}
```

**拿它做增量基线会得到完全错误的排名**。所以本项目里没有它。

同理，Trending 页面只用作「候选发现」的辅助渠道（且解析失败不影响主流程），
**绝不作为任何数值的来源**。

---

## 本地快速开始

```bash
cd star-pulse

# 1. 配置 Token（不配也能跑，但只有 60 次/小时，采不了几个仓库）
cp .env.example .env
#   编辑 .env，填入 GITHUB_TOKEN
#   生成地址：https://github.com/settings/personal-access-tokens/new
#   类型选 Fine-grained，权限只需 Public Repositories -> Metadata: Read

# 2. 体检：确认配置、配额、数据源连通性
python -m star_pulse doctor

# 3. 先小规模试跑，验证链路（约 1 分钟）
python -m star_pulse run-daily --limit 30

# 4. 出报告（此刻还没有历史，会输出「新项目榜 + 预热说明」）
python -m star_pulse report --period this-week

# 5. 之后每天跑一次 run-daily；满 8 天后跑 report --period last-week 就有周增榜了
```

### 常用命令

| 命令 | 作用 |
|---|---|
| `python -m star_pulse doctor` | 体检：配置、配额、三个数据源的连通性 |
| `python -m star_pulse run-daily` | 每日采集（候选发现 + 快照）。**每天跑这个** |
| `python -m star_pulse run-daily --limit 30` | 小规模试跑 |
| `python -m star_pulse snapshot --repos a/b c/d` | 给指定仓库拍快照，用于验证 |
| `python -m star_pulse report --period last-week` | 生成周报。**每周跑这个** |
| `python -m star_pulse report --period 2026-09-06:2026-09-13` | 任意区间 |
| `python -m star_pulse stats` | 查看快照覆盖情况 |
| `python -m star_pulse rebuild` | 从 JSON 快照重建 SQLite |
| `python tests/test_pipeline.py` | 回归测试（用已发布榜单做基准） |

---

## 同步到 GitHub 并让它每周自动跑

### 1. 推到 GitHub

```bash
cd star-pulse
git init
git add .
git commit -m "feat: star-pulse 初始版本"

# 在 GitHub 上新建一个仓库（建议 Private），然后：
git remote add origin https://github.com/<你的用户名>/star-pulse.git
git branch -M main
git push -u origin main
```

### 2. 打开 Actions

推上去之后，`.github/workflows/` 下两条工作流就位，**不需要做任何额外配置**：

| 工作流 | 触发时间 | 做什么 |
|---|---|---|
| `daily snapshot` | 每天 04:00（UTC+8） | 采集快照并提交 `data/snapshots/` |
| `weekly report` | 每周一 10:00（UTC+8） | 跑回归测试 → 生成周报 → 提交 `reports/` |

两条都带 `workflow_dispatch`，可以在 Actions 页面点 **Run workflow** 手动补跑。

Token 用 Actions 内置的 `secrets.GITHUB_TOKEN`，对单个仓库有 **1000 次/小时** 配额，
默认候选池 800 个足够，**不需要另外配置 PAT**。

### 3. 想启用 LLM 中文解读（可选）

仓库 **Settings → Secrets and variables → Actions** 里添加三个 secret：

```
LLM_BASE_URL   https://api.openai.com/v1
LLM_API_KEY    sk-...
LLM_MODEL      gpt-4o-mini
```

不配也不影响任何功能，只是少一段中文解读。任何 LLM 调用失败都会被吞掉，不会让报告生成失败。

### 4. 重要提醒

- ⚠️ **定时工作流会在仓库连续 60 天无活动后被 GitHub 自动禁用。** 本项目的每日提交本身构成仓库活动，通常能维持；但建议每两个月去 Actions 页面确认一下状态。
- `GITHUB_TOKEN` 推送的提交**不会触发**其他工作流（GitHub 的有意设计），所以不必担心循环触发，`[skip ci]` 只是显式保险。
- 首次推送后，**第 8 天**才会有第一份真正的周增榜。在此之前报告会如实标注「本期无增量榜」。

---

## 报告里有什么

每期报告输出三份文件：`reports/<周>_<起>_<止>.md` / `.html` / `.json`。

- **本周新增 Star Top N** — 周增排名，附语言、分类、是否疑似刷星
- **新项目榜** — 周期内创建的项目按当前总星排序（**口径不同**：衡量新项目起跑速度，不是存量增长）
- **分类透视** — Agent Skill / 可视化 / 输出规范 / 本地优先 等占比
- **数据质量** — 快照覆盖天数、被剔除的仓库及原因、疑似刷星标记

`html` 版本是自包含单文件（内嵌 Chart.js），可以直接丢给别人看。
`reports/LATEST.md` 始终是最新一期，方便在 GitHub 上直接浏览。

### 三道质量闸门

1. **跨度闸门**：增量必须取「不晚于目标日期的最近一次快照」。跨度超过 `max_span_days`（默认 10 天）的仓库**直接剔除**，并在报告里说明原因 —— 避免 14 天的增量混进 7 天的榜里。
2. **刷星启发式**：星叉比 > 300、单日跳变 > 近 7 日均值 10 倍、新库暴星、叉数过低。**只标注，不剔除**，把判断权交回给读者。
3. **数值与叙述分离**：所有数字来自 SQL；LLM 只写文字，不参与计算。

---

## 项目结构

```
star-pulse/
├── config/
│   ├── settings.toml          # 候选池、报告口径、网络参数
│   └── watchlist.txt          # 固定关注的仓库
├── star_pulse/
│   ├── cli.py                 # 命令行入口
│   ├── config.py              # 配置加载 + 配额预检
│   ├── net.py                 # 限流感知 HTTP 客户端（核心）
│   ├── github_api.py          # REST /repos + Search API
│   ├── trending.py            # Trending HTML 解析（尽力而为）
│   ├── pipeline.py            # 采集流水线
│   ├── db.py                  # SQLite + JSON 快照
│   ├── analyze.py             # 增量计算、防刷星、分类
│   ├── render.py              # Markdown / HTML 报告
│   └── llm.py                 # 可选解读
├── tests/test_pipeline.py     # 回归测试
├── examples/reports/          # 示例报告（用已发布的 2026-W37 数据生成）
├── data/snapshots/*.json      # 快照事实来源（提交进 git）
├── reports/                   # 产出报告
└── .github/workflows/         # daily.yml / weekly.yml
```

### 数据存哪里

**`data/snapshots/YYYY-MM-DD.json` 是事实来源**，纯文本、可读、diff 友好，提交进 git。

SQLite（`data/pulse.db`）只是为了查询方便，**不提交**。CI 每次都是全新环境，
启动时会自动把 JSON 回灌进 SQLite（`python -m star_pulse rebuild` 可以手动重建）。
换句话说：SQLite 随时可以从 JSON 重建，JSON 丢了才是真丢了。

---

## 调参

编辑 `config/settings.toml`：

| 参数 | 默认 | 说明 |
|---|---|---|
| `max_candidates` | 800 | 候选池上限。认证后配额 5000/小时，800 有大量富余 |
| `search_lookback_days` | 14 | 搜索「近期创建」的回溯天数 |
| `search_min_stars` | 50 | 新项目的星数门槛 |
| `languages` | `["All"]` | 语言过滤，可填 `["Python","TypeScript"]` |
| `top_n` | 20 | 榜单长度 |
| `tz_offset_hours` | 8 | 报告日期归属时区 |
| `max_span_days` | 10 | 增量跨度上限，超出即剔除 |
| `request_gap_seconds` | 0.15 | 请求间隔，用于规避二级限流 |

`config/watchlist.txt` 里放你真正关心的仓库，它们每天都会被拍快照，
不受 Trending 和搜索结果波动的影响。

---

## 已知限制

- **第 8 天才出周增榜**。这是「GitHub 无增量接口」的直接后果，无法绕过。
- **Trending 解析依赖 HTML 结构**。GitHub 改版会导致该渠道失效；届时解析器会打警告并返回空列表，Search API 自动兜底，主流程不受影响。
- **分类基于关键词**，是有意为之（可回归测试、无幻觉）。它只影响可读性，不影响排名。想更准可以接 LLM，但请注意别让它碰到数字。
- **星数快照有采样时点偏差**。每天固定时刻采一次，不是全天精确积分，这是所有同类周榜的共同特征。
