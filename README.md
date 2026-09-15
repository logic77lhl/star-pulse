# star-pulse

本地生成「GitHub 星耀榜」：每日采快照，每周出周报（含周增 Star 排名、分类透视、防刷星标记），并自动生成一个**每天情况汇总的在线看板**。

零第三方依赖，只用 Python 3.11+ 标准库。CI 里不需要 `pip install`。

> 在线看板：https://logic77lhl.github.io/star-pulse/

---

## 技术路线

一句话：**不依赖任何第三方增量数据，自己每天拍快照，用两个时间点相减。**
整条链路是「发现候选 → 拍快照 → 落库 → 出看板 / 周报」，全程只用 Python 标准库。

```
                    ┌─ ① 种子清单（config/watchlist.txt）  ← 你显式关注，必进
                    ├─ ② Trending 页解析                   ← 当期热点
   发现候选 ────────┤
                    ├─ ③ 池内续期（保底名额）              ← 让老项目的时间序列不断
                    └─ ④ Search API 搜索新项目             ← 填满剩余名额
                                  │
                                  ▼
                    候选池（上限 max_candidates，默认 800）
                                  │
                                  ▼
                   每日快照 → data/snapshots/*.json        ← 事实来源，提交进 git
                                  │
                       启动时回灌 ▼
                          SQLite（本地查询缓存，不提交）
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
             docs/ 在线看板                  reports/ 周报
             （每天 04:00）                  （每周一 10:00）
```

看板上的行数不是固定值，每天重算，且是**滚动窗口而非累积清单**：

```
候选池 800  −  已归档/ fork  =  看板渲染行数（当前 799）
```

取数条件是「只取当天快照的仓库」（`analyze.project_rows` 里 `s.snap_date = :day`），
所以看板的明细不会随历史累积而无限膨胀；`repo` 表本身是累积的，那是为了算增量。

### 四个发现渠道，顺序即优先级

请求预算有限，谁先来谁占名额。顺序不是随便排的，是按「谁更不可替代」：

| 顺序 | 渠道 | 为什么在这个位置 |
|---|---|---|
| ① | 种子清单 | 用户显式关注的项目，**必须**进池，排最前且不设上限 |
| ② | Trending 页 | 当期热点，**只有这一个渠道**能提供 |
| ③ | 池内续期 | 见下方「为什么必须有保底名额」 |
| ④ | Search API | 垫底，用剩下的名额发现新项目 |

### 为什么必须有「池内续期」保底名额

Search API 只能搜「最近 N 天创建」的仓库（`search_lookback_days`，默认 14 天）。
这意味着 **一个项目滑出这个时间窗口后，就再也没有任何渠道能发现它**。

若让搜索先吃满名额，老项目会静默掉出候选池 —— 当天拿不到快照，星数增量随之中断，
快照序列永久缺一格，而且**日志里一行错误都不会有**。这正是「时间序列断档」的成因。

所以渠道③排在搜索之前，先把 `pool_refresh_reserve`（默认 200）个名额续上，
剩下的才交给搜索。想更偏向新项目就调小它；设 0 即退回旧的「搜索优先」行为。

> 续期的排序**必须是「已追踪最久」而不是「最近发现」**（见 `db.pool_refresh_repos`）：
> 后者排在最前面的恰恰是刚发现的仓库，它们还在搜索窗口内、本来就会被找到，
> 占着保底名额却不解决任何问题。

### 数据分层

| 层 | 位置 | 进 git？ | 作用 |
|---|---|---|---|
| 事实来源 | `data/snapshots/YYYY-MM-DD.json` | ✅ | 每日快照，纯文本、可读、diff 友好 |
| 查询缓存 | `data/pulse.db`（SQLite） | ❌ | 只为查询方便，随时可从 JSON 重建 |
| 翻译缓存 | `data/i18n/zh.json` | ✅ | 每个仓库只翻一次，省 LLM 费用 |
| 站点产物 | `docs/` | ✅ | Pages 直接从这里部署 |

**只有 JSON 是真的丢了就丢了**，SQLite 随时可以 `python -m star_pulse rebuild` 重建。
CI 每次都是全新环境，所以「启动时从 JSON 回灌」不是可选优化，而是历史能跨运行存活的前提。

### 三条不可让渡的原则

1. **数值全部来自 SQL。** LLM 只写文字，永远不参与计算 —— 保证可复现、无幻觉。
2. **分类用确定性关键词规则。** 可回归测试、零成本、每天重跑结果一致。代价是有长尾
   （约四成仓库落在「其他」），所以页面上如实标注「类目为规则推断」，不吹成「智能分类」。
3. **唯一适合 LLM 的地方是翻译简介，而且必须落缓存。** 以 `repo_id` 为键、随仓库提交，
   同一个项目一辈子只翻一次；未命中的行回退英文原文，绝不留白。

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
| `python -m star_pulse site` | 生成 `docs/` 在线看板（Pages 用） |
| `python -m star_pulse translate` | 补齐缺失的中文简介翻译（增量，已缓存的不会重翻） |
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

# 在 GitHub 上新建一个公开仓库（Pages 在免费账号下只支持公开仓库）
# 用 gh 一条命令搞定：
gh repo create star-pulse --public --source=. --remote=origin --push
```

### 2. 打开 Actions

推上去之后，`.github/workflows/` 下两条工作流就位，**不需要做任何额外配置**：

Token 用的是 Actions 内置的 `secrets.GITHUB_TOKEN`，对单个仓库有 **1000 次/小时** 配额，
默认候选池 800 个足够，**不需要另外配置 PAT**。

| 工作流 | 触发时间 | 做什么 |
|---|---|---|
| `daily snapshot` | 每天 04:00（UTC+8） | 采集快照 → 重建看板 → 提交 `data/snapshots/` 与 `docs/` |
| `weekly report` | 每周一 10:00（UTC+8） | 跑回归测试 → 生成周报 → 归档并重建看板 → 提交 |

两条都带 `workflow_dispatch`，可以在 Actions 页面点 **Run workflow** 手动补跑。

### 3. 打开 GitHub Pages（看板地址）

仓库 **Settings → Pages**：

- **Source** 选 `Deploy from a branch`
- **Branch** 选 `main`，**Folder** 选 `/docs`
- 保存后约 1 分钟，看板就在 `https://<你的用户名>.github.io/star-pulse/` 上线

> 为什么用「分支部署」而不是 `upload-pages-artifact`：
> `GITHUB_TOKEN` 推送的提交**不会触发**其他工作流（GitHub 的有意设计），
> 所以「push 触发 Pages 部署工作流」这条路根本不会启动。
> 直接从分支`/docs` 部署就没有这个问题——提交即上线，不需要额外工作流。
>
> 注意：GitHub Pages 对**免费账号只在公开仓库上可用**。

也可以用命令行一步配好：

```bash
gh api -X POST /repos/<你的用户名>/star-pulse/pages \
  -f "source[branch]=main" -f "source[path]=/docs"
```

### 4. 想启用 LLM 中文解读（可选）

仓库 **Settings → Secrets and variables → Actions** 里添加三个 secret：

```
LLM_BASE_URL   https://api.openai.com/v1
LLM_API_KEY    sk-...
LLM_MODEL      gpt-4o-mini
```

不配也不影响任何功能，只是少一段中文解读。任何 LLM 调用失败都会被吞掉，不会让报告生成失败。

### 5. 重要提醒

- ⚠️ **定时工作流会在仓库连续 60 天无活动后被 GitHub 自动禁用。** 本项目的每日提交本身构成仓库活动，通常能维持；但建议每两个月去 Actions 页面确认一下状态。
- `GITHUB_TOKEN` 推送的提交**不会触发**其他工作流（GitHub 的有意设计），所以不必担心循环触发，`[skip ci]` 只是显式保险。
- 首次推送后，**第 8 天**才会有第一份真正的周增榜。在此之前报告会如实标注「本期无增量榜」。

---

## 在线看板（每天的情况）

`python -m star_pulse site` 会生成 `docs/`，部署到 Pages 后就是你的看板。

版面取向是**项目名单在前、趋势在后** —— 看板的主用途是「看有哪些项目、各自什么情况」，
趋势只是佐证，所以趋势部分收进折叠块，默认不占屏。

| 板块 | 内容 |
|---|---|
| 概览卡片 | 项目名单数、中文简介覆盖率、累计追踪数、覆盖语言数、快照天数 |
| **项目名单**（默认展开） | **全部追踪仓库**，含星数 / Fork / 语言 / 当日新增 / **中文类目** / **中文简介**；支持按项目名与简介搜索、按类目或语言筛选、按星数·新增·Fork·创建时间排序、按每屏 50 / 100 / 300 / 全部切换显示条数 |
| 趋势与榜单（默认收起） | 折叠块，展开后是下面四项 |
| ├ 每天的情况 | 逐日表：当天入库仓库数 / 新增星数合计 / 上涨仓库数 / **当日涨幅冠军** / 新进候选数 |
| ├ 当日涨幅榜 | 最新一天 Top 15（单日维度，不是周维度） |
| ├ 累计增长榜 | 区间内 Top 15 |
| ├ 每日新增走势 | 柱状图：每天全部追踪仓库的新增星数合计，一眼看出「今天热不热闹」 |
| └ 领涨仓库走势 | 折线图：区间累计增量最大的 8 个仓库的**相对首日累计增量**（不用绝对星数，否则 3 万星和 26 万星没法画在同一张图上） |
| 历史周报 | 归档链接 |

两个实现上的坑，改版面时别踩回去：

1. **折叠区里的 canvas 必须等展开后再建。** 闭合的 `<details>` 里 canvas 量到的是 0×0，
   Chart.js 会照着 0 尺寸初始化，展开后就是一片空白。所以图表是在 `toggle` 事件里懒创建的。
2. **项目名单每行独占一行，且简介只存一份。** 整张表挤成一行（约 400 KB 单行）会让 git
   完全没法做增量压缩，每天都要重存一份完整 blob；把简介再抄一份进 `data-desc` 属性则会
   把页面从 ~400 KB 撑到 500 KB。两条都有回归测试兜着。

**行全部在页面里，但默认只显示前 100 条。** 一屏铺 800 行没人看得下去，所以加了个
「每屏条数」控件（默认 100，可切 50 / 300 / 全部）。要注意的是**服务端仍然全量渲染**，
显示限制只由 JS 控制 `hidden` —— 搜索 / 筛选 / 排序覆盖的始终是全量 800 行，
不会出现「搜不到」的情况；停用 JS 时页面也仍是一份完整可读的名单。
统计文案会提示「另有 N 个符合条件，调大显示条数或搜索即可看到」，避免误以为只有 100 个。

### 中文层

看板面向中文读者做了两层本地化，两者都**只影响可读性，不参与任何数值计算**：

- **类目列**：由 `analyze.classify` 的关键词规则判定，确定性、可回归测试。
  长尾是真实存在的（约四成落在「其他」），页面上如实标注口径。
- **中文简介**：`star_pulse/i18n.py` 调 LLM 批量翻译后写入 `data/i18n/zh.json`。
  缓存以 **`repo_id` 为键**（不用 `full_name` —— 改名会让缓存失效），随仓库提交，
  并在 CI 里复用。所以同一仓库一辈子只翻一次，日常运行零 LLM 成本。
  未命中缓存或未配置 LLM 时回退英文原文；翻译失败**绝不写出空缓存文件**。

新增/遗漏的翻译用一条命令补齐：

```bash
python -m star_pulse translate            # 只翻缓存里没有的
python -m star_pulse translate --limit 50 --batch 20
```

没配 LLM 时它会明确报错并返回非零退出码，而不是静默返回零条。

看板的图表数据直接内联在 HTML 里，不依赖 fetch，本地双击打开也能看。
（唯一的例外是 Chart.js 本身走 CDN；取不到时会降级成一行提示文字，表格数据不受影响。）
同时输出 `docs/data.json` 供二次消费。

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
│   ├── pipeline.py            # 采集流水线（渠道优先级在这里）
│   ├── db.py                  # SQLite + JSON 快照
│   ├── analyze.py             # 增量计算、防刷星、分类、日维度汇总
│   ├── render.py              # Markdown / HTML 周报
│   ├── site.py                # docs/ 在线看板生成
│   ├── i18n.py                # 中文简介翻译 + 本地缓存（只产文字，不碰数值）
│   └── llm.py                 # 可选解读
├── tests/test_pipeline.py     # 回归测试
├── examples/reports/          # 示例周报（用已发布的 2026-W37 数据生成）
├── data/snapshots/*.json      # 快照事实来源（提交进 git）
├── data/i18n/zh.json          # 中文简介缓存（提交进 git，按 repo_id 命中）
├── reports/                   # 周报产出
├── docs/                      # Pages 站点（提交进 git）
└── .github/workflows/         # daily.yml / weekly.yml
```

### 数据存哪里

一张表说清（细节见上方「技术路线 → 数据分层」）：

| 进 git | 文件 | 丢了会怎样 |
|---|---|---|
| ✅ | `data/snapshots/*.json` | **真丢了** —— 这是唯一的事实来源 |
| ✅ | `data/i18n/zh.json` | 中文简介全丢，得重新花钱翻一遍 |
| ❌ | `data/pulse.db` | 无所谓，`python -m star_pulse rebuild` 从 JSON 重建 |
| ✅ | `docs/` | 无所谓，`python -m star_pulse site` 重新生成 |

CI 每次都是全新环境，启动时会自动把 JSON 回灌进 SQLite —— 这就是历史能跨运行存活的原因。

---

## 调参

编辑 `config/settings.toml`：

| 参数 | 默认 | 说明 |
|---|---|---|
| `max_candidates` | 800 | 候选池上限。认证后配额 5000/小时，800 有大量富余 |
| `pool_refresh_reserve` | 200 | 给「池内续期」预留的保底名额。防止老项目滑出搜索窗口后掉出候选池、时间序列断档。设 0 退回「搜索优先」 |
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
- **看板是滚动窗口，不是累积清单**。每天只渲染「当天快照」的仓库，所以行数会在
  `max_candidates` 附近小幅波动（当前 799 = 800 − 1 个已归档），**不会**随历史累积无限增长。
  历史快照仍然全部留在 `data/snapshots/`，只是不铺在看板上。
- **中文简介依赖 LLM**。没配 `LLM_*` 时不会自动新增翻译，已缓存的部分照常显示，
  其余行回退英文原文 —— 页面始终是完整的，不会出现空洞。
- **仓库体积会持续增长**。候选池 800 时，每天的 JSON 快照约 180 KB，一年约 60 MB。
  如果在意体积，把 `max_candidates` 降到 300–500（一年约 25–35 MB），
  或定期归档 `data/snapshots/` 里一年以上的文件。
