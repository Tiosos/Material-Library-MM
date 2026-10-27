# Material Library - MM

> 通用材料库（Universal Material Library）—— 一份**独立于业务系统的材料主数据（MDM）**方案与可运行原型。
> 可以单独当查询站，也可以被 joineryflow 或其它系统引用 / 导入。

*A vendor-neutral material master-data library with forgiving, "type-half-of-it-and-still-find-it" search. Ships as a spec (schema + mapping) plus a zero-dependency prototype.*

参考来源：[`github.com/Tiosos/joineryflow`](https://github.com/Tiosos/joineryflow)（细木工生产系统，Catalog 模块 9 张材料表、14 种产品类型）。

---

## 目录

```
.
├── README.md                    本文件
├── LICENSE                      MIT
├── docs/方案-v1.md              ★ 方案正文（看这个）
├── schema/
│   ├── material.sql            规范 DDL（PostgreSQL，含 tsvector 加权 + pg_trgm）
│   └── material.schema.json    UMR 交换格式 JSON Schema（契约）
├── mapping/
│   └── joineryflow.yaml        9 张 catalog 表 → UMR 的字段映射、回填、同步规范
└── proto/                       ★ 可运行原型（零依赖，验证搜索手感）
    ├── build_db.py             生成示例库（100 条，真实品牌与字段结构）
    ├── search.py               分级降级检索实现
    ├── serve.py                HTTP 服务（查询 / 详情 / 导出）
    ├── data/materials.jsonl    UMR 交换格式样例
    └── web/index.html          查询界面
```

---

## 快速开始（原型）

```bash
cd proto
python3 build_db.py     # 生成 materials.sqlite + data/materials.jsonl
python3 serve.py 8848   # http://localhost:8848
```

零依赖：只用 Python 3 标准库，不需要 pip install。

打开后直接搜，这几个查询用来验证"缺省搜索"的四级降级：

| 输入 | 走哪一级 | 说明 |
|---|---|---|
| `blum soft close hinge` | 全文 AND | 多词全中 |
| `blu` | 前缀 | 没打完也算 |
| `blum hinge 450` | 放宽 | 自动丢掉没有命中的词，不再返回空 |
| `luminux` | 模糊 | 拼错 → 找回 Laminex，并提示"你是不是要找 laminex" |
| `tempered glass` | 同义词 | 俗称 → toughened safety glass |
| `台面` | 同义词 | 中文俗称 → benchtop |
| `thickness:18` | 属性过滤 | 不记得名字，只记得规格 |
| `blum -hinge` | 排除 | 排除某类 |
| `mdf 18 white` | 全文 AND | 俗称 + 规格混搜 |

界面顶部的状态条会告诉你**这次用了哪一级检索**，这是判断手感对不对的关键。

---

## HTTP 接口

| 接口 | 说明 |
|---|---|
| `GET /api/search?q=&category=&brand=&supplier=&limit=` | 检索，返回命中列表 + 本次降级级别 + 拼写建议 |
| `GET /api/material/{uid}` | 单条详情（含属性、价格历史、替代料、编码簿） |
| `GET /api/export?q=&format=jsonl\|csv` | 导出为 UMR 交换格式或 CSV |
| `GET /api/suggest?q=` | 输入补全 |

`GET /api/search` 返回示例（节选）：

```json
{
  "level": "relaxed",
  "total": 3,
  "suggestion": null,
  "results": [{ "material_uid": "mat_01...", "name": "Blumotion Soft Close Hinge", "score": 7.4 }]
}
```

`level` 是这次检索实际用到的级别，`suggestion` 是"你是不是要找 ___"。

---

## 核心思路（一句话版）

> 把 joineryflow 的 **9 张材料表合并成 1 张 core 表 + 类型画像 + 属性 KV**，给每条材料一个**跨系统永久稳定的 `material_uid`**，再用**四级降级的检索**取代现在的单关键词 `ILIKE`。

详见 [`docs/方案-v1.md`](docs/方案-v1.md)。

---

## 想改原型的数据

改 `proto/build_db.py` 顶部的素材池（`BOARDS` / `HARDWARE` / `EDGING` / `GLASS` / `TIMBER` / `BENCHTOP` / `APPLIANCE` / `CUSTOM` / `HIRE` / `CONSUMABLE`），重跑 `python3 build_db.py` 即可。

`SYNONYM_HINTS` 是同义词表——"缺省搜索"能兜住俗称和中文，全靠它。

---

## 当前状态

- [x] 方案 v1（数据模型 / 搜索 / 集成 / 路线图）
- [x] 数据字典：DDL + JSON Schema
- [x] joineryflow 映射规范
- [x] 可运行搜索原型（SQLite FTS5，100 条示例数据）
- [ ] 真实数据 ETL（需要 joineryflow 库连接或一个导出快照）
- [ ] PostgreSQL 版实现（tsvector 加权 + pg_trgm）
- [ ] write-back 到 joineryflow
- [ ] SDK / MCP server

**下一步取决于你对方案第 10 章那 6 个问题的回答。**

---

## License

[MIT](LICENSE) — 方案文档、数据模型与原型代码均可自由使用与修改。

原型示例数据中的品牌名（Blum、Hettich、Laminex、Caesarstone 等）仅用于演示字段结构与搜索行为，与各自商标持有人无关，不含真实价格与商务条款。
