"""star-pulse：本地 GitHub 星耀榜生成器。

设计要点
  · 零第三方依赖，只用 Python 标准库，CI 里不需要 pip install
  · 数值全部来自每日快照相减，LLM 不参与任何计算
  · 数据以 data/snapshots/*.json 为事实来源，SQLite 只是可重建的查询缓存
"""

__version__ = "1.0.0"
