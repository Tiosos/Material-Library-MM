-- =============================================================================
-- Material Library — Universal Material Record (UMR)  v1
-- 目标库: PostgreSQL 14+  (需要 pg_trgm / unaccent 扩展；全文检索用内置 tsvector)
--
-- 设计原则
--   1. 一张 core 表承载"所有材料都有的东西"，不再按材料类型分表
--      （joineryflow 现状是 9 张物理表 board/glass/timber/hardware/edging/
--       custom_made/benchtop/appliance/hire，跨类型检索要扫 9 次）
--   2. 类型差异走 material_profile（受控字段定义）+ material_attribute（键值）
--      既能校验，又不用为第 10 种材料改 schema
--   3. 身份与引用走 material_uid（跨系统稳定），SKU/供应商码等走 material_identifier
--   4. 价格可追溯（价格是历史，不是当前值）
--   5. 来源可溯源（每条记录知道它从哪个系统的哪个 id 来）
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- ---------------------------------------------------------------------------
-- 1. 枚举域（用 CHECK 而不是真 enum，方便新增类型不改类型定义）
-- ---------------------------------------------------------------------------

-- 材料大类：与 joineryflow 的 9 张表一一对应，另留 generic 兜底
--   board      板材 / 层压板      (board_materials)
--   edging     封边条            (edging_materials)
--   glass      玻璃              (glass_materials)
--   timber     实木              (timber_materials)
--   benchtop   台面 / 石面        (benchtop_materials)
--   hardware   五金              (hardware_materials)
--   appliance  电器              (appliances)
--   custom     定制外协           (custom_made)
--   hire       设备租赁           (equipment_hire)
--   consumable 耗材 / 油漆 / 胶水  (joineryflow 中挂在 Supplies / Paint 页签下)

CREATE TABLE material (
    -- ---- 身份 -------------------------------------------------------------
    material_uid     text PRIMARY KEY,          -- 'mat_01J8Z...'  ULID，跨系统永久稳定
    rev              integer     NOT NULL DEFAULT 1,
    status           text        NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','draft','archived','superseded')),
    superseded_by    text REFERENCES material(material_uid),

    -- ---- 描述 -------------------------------------------------------------
    category         text        NOT NULL,      -- 上面的大类，写 CHECK 见下
    product_type     text,                      -- 细分类型（joineryflow 的 14 种页签）
    name             text        NOT NULL,      -- 显示名，人读
    description      text,                      -- 长描述
    brand            text,                      -- 品牌 / 制造商（Miele、Blum、Laminex…）
    model_no         text,                      -- 型号（appliance.model_number）
    search_text      text,                      -- 由触发器拼成的检索文本（见 §6）

    -- ---- 商务 -------------------------------------------------------------
    default_supplier text,                      -- 默认供应商名（冗余，便于搜索）
    default_supplier_ref text,                  -- 供应商侧的编码
    lead_time_days   integer,
    unit             text,                      -- 计量单位 UM：sheet / ea / m / m2 / L / roll
    currency         char(3)     NOT NULL DEFAULT 'AUD',
    unit_cost        numeric(14,4),             -- 当前不含税单价（缓存值，真源在 material_price）
    cost_basis       text        NOT NULL DEFAULT 'ex_gst'
                     CHECK (cost_basis IN ('ex_gst','inc_gst','ex_tax','inc_tax')),

    -- ---- 媒体 / 链接 ------------------------------------------------------
    image_url        text,
    product_url      text,
    attachments      jsonb       NOT NULL DEFAULT '[]'::jsonb,   -- [{kind,url,title}]

    -- ---- 自由扩展 ---------------------------------------------------------
    attributes       jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- 类型专属字段，受 profile 校验
    tags             text[]      NOT NULL DEFAULT '{}',
    synonyms         text[]      NOT NULL DEFAULT '{}',          -- 别名/俗称，参与检索

    -- ---- 溯源 -------------------------------------------------------------
    source_system    text,                      -- 'joineryflow' / 'filemaker' / 'manual'
    source_id        text,                      -- 来源系统的主键（旧 id）
    source_payload   jsonb,                     -- 原始行快照，便于回滚与对账
    imported_at      timestamptz,
    confidence       numeric(3,2) NOT NULL DEFAULT 1.00,         -- 数据可信度 0~1

    -- ---- 审计 -------------------------------------------------------------
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    created_by       text,
    updated_by       text,

    CONSTRAINT material_category_ck CHECK (category IN (
        'board','edging','glass','timber','benchtop',
        'hardware','appliance','custom','hire','consumable','generic'))
);

COMMENT ON COLUMN material.material_uid IS
    '跨系统稳定的主键。外部系统一律存这个，不要存本地自增 id。';
COMMENT ON COLUMN material.attributes IS
    '类型专属字段（board 的 colour/thickness_mm，glass 的 glass_type…）。键集合由 material_profile 约束。';
COMMENT ON COLUMN material.synonyms IS
    '同义词与俗称。搜索"melamine"能命中 "Laminate Board" 全靠它。';

CREATE INDEX material_category_idx    ON material (category) WHERE status <> 'archived';
CREATE INDEX material_product_type_idx ON material (product_type) WHERE status <> 'archived';
CREATE INDEX material_supplier_idx    ON material (default_supplier);
CREATE INDEX material_updated_idx     ON material (updated_at);          -- 增量同步 ?since=
CREATE INDEX material_source_idx      ON material (source_system, source_id);
CREATE INDEX material_attributes_idx  ON material USING gin (attributes jsonb_path_ops);
CREATE INDEX material_tags_idx        ON material USING gin (tags);
CREATE INDEX material_synonyms_idx    ON material USING gin (synonyms);

-- ---------------------------------------------------------------------------
-- 2. 标识符：一个材料可以有任意多个"别人家的编码"
--    这是"被其他系统引用/导入"的关键——外部系统拿自己的码来问，也能命中
-- ---------------------------------------------------------------------------

CREATE TABLE material_identifier (
    material_uid  text NOT NULL REFERENCES material(material_uid) ON DELETE CASCADE,
    kind          text NOT NULL,   -- sku / barcode / ean / supplier_code / legacy_id /
                                   -- catalog_code / internal_ref / slab_id / model_no / cv_code
    value         text NOT NULL,
    source_system text,            -- 这个编码属于谁（'joineryflow','cv','supplier:CDK Stone'）
    is_primary    boolean NOT NULL DEFAULT false,
    PRIMARY KEY (kind, value, source_system),
    UNIQUE (material_uid, kind, source_system)   -- 一个来源一种编码只能有一个
);

CREATE INDEX material_identifier_uid_idx ON material_identifier (material_uid);
CREATE INDEX material_identifier_val_idx ON material_identifier (lower(value) varchar_pattern_ops);

COMMENT ON TABLE material_identifier IS
    '跨系统编码簿。joineryflow 里散落在 9 张表的 sku/code/internal_ref/slab_id/'
    'model_number/contract_ref 在这里统一成一类数据。';

-- ---------------------------------------------------------------------------
-- 3. 类型画像：定义每种材料"应该有哪些专属字段"
--    新增材料类型 = 插一行 profile，不改表结构
-- ---------------------------------------------------------------------------

CREATE TABLE material_profile (
    category     text PRIMARY KEY,
    display_name text NOT NULL,
    field_schema jsonb NOT NULL DEFAULT '{}'::jsonb,   -- JSON Schema（Draft 2020-12）
    required     text[] NOT NULL DEFAULT '{}',
    search_weight jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {"thickness_mm": 2, "colour": 3}
    unit_options text[] NOT NULL DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- 4. 价格历史：单价是"某一时刻的事实"，不是材料的一个属性
-- ---------------------------------------------------------------------------

CREATE TABLE material_price (
    price_id      bigserial PRIMARY KEY,
    material_uid  text NOT NULL REFERENCES material(material_uid) ON DELETE CASCADE,
    supplier      text,
    unit_cost     numeric(14,4) NOT NULL,
    currency      char(3) NOT NULL DEFAULT 'AUD',
    cost_basis    text NOT NULL DEFAULT 'ex_gst',
    qty_break     numeric(12,3),        -- 阶梯价：起订量
    valid_from    date NOT NULL DEFAULT CURRENT_DATE,
    valid_to      date,
    source_system text,
    recorded_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX material_price_uid_idx ON material_price (material_uid, valid_from DESC);

-- ---------------------------------------------------------------------------
-- 5. 关系：替代料 / 配套 / 组合件 —— 缺料时能立刻给出可替代项
-- ---------------------------------------------------------------------------

CREATE TABLE material_relation (
    from_uid    text NOT NULL REFERENCES material(material_uid) ON DELETE CASCADE,
    to_uid      text NOT NULL REFERENCES material(material_uid) ON DELETE CASCADE,
    kind        text NOT NULL,   -- substitute / accessory / supersedes / bundled_with / similar
    note        text,
    bidirectional boolean NOT NULL DEFAULT true,
    PRIMARY KEY (from_uid, to_uid, kind)
);

CREATE INDEX material_relation_to_idx ON material_relation (to_uid, kind);

-- ---------------------------------------------------------------------------
-- 6. 检索向量：一个 search_text 列 + 生成列 tsv，权重分四级
--    A = 编码类（最精确）  B = 名称/品牌/型号
--    C = 供应商/类型/单位   D = 描述/属性/同义词/标签
-- ---------------------------------------------------------------------------

ALTER TABLE material ADD COLUMN search_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(
            (SELECT string_agg(i.value, ' ') FROM material_identifier i
              WHERE i.material_uid = material.material_uid), '')), 'A') ||
        setweight(to_tsvector('simple',
            coalesce(name,'') || ' ' || coalesce(brand,'') || ' ' || coalesce(model_no,'')), 'B') ||
        setweight(to_tsvector('simple',
            coalesce(default_supplier,'') || ' ' || coalesce(product_type,'') || ' '
            || coalesce(category,'') || ' ' || coalesce(unit,'')), 'C') ||
        setweight(to_tsvector('simple',
            coalesce(description,'') || ' ' || coalesce(attributes::text,'') || ' '
            || coalesce(array_to_string(synonyms,' '),'') || ' '
            || coalesce(array_to_string(tags,' '),'')), 'D')
    ) STORED;

CREATE INDEX material_tsv_idx     ON material USING gin (search_tsv);
CREATE INDEX material_trgm_idx    ON material USING gin (name gin_trgm_ops);
CREATE INDEX material_desc_trgm_idx ON material USING gin (description gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- 7. 检索统计：给"常用度"排序提供依据（搜索量、被引用次数）
-- ---------------------------------------------------------------------------

CREATE TABLE material_usage (
    material_uid text PRIMARY KEY REFERENCES material(material_uid) ON DELETE CASCADE,
    ref_count    integer NOT NULL DEFAULT 0,   -- 被多少个订单/报价/项目引用过
    search_hits  integer NOT NULL DEFAULT 0,
    last_used_at timestamptz
);
