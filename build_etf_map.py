"""自动生成申万三级行业 → ETF 映射表。

数据源: 新浪 ETF 列表 (akshare fund_etf_category_sina)，失败时回退东财。
匹配规则: 优先匹配申万三级行业名/别名；没有合适产品时按三级 → 二级 → 一级回退，
          每个行业最多保留 3 只候选，并保留匹配层级供前端提示。
手工介入: 直接编辑 data 目录下的 industry_etf_map_sw3.json：
          - aliases 段补充行业别名（如 "半导体": ["芯片"]）
          - overrides 段手工指定某行业的 ETF（[] 表示该行业显式不推荐）
          重新运行时 overrides 和 aliases 的修改会被保留。

用法: python build_etf_map.py
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime

import akshare as ak

from db import get_db
from runtime_paths import data_path, resource_path

MAP_FILE = "industry_etf_map_sw3.json"
TAXONOMY_FILE = "industry_taxonomy.json"
SCHEME = "sw3"
MAX_CANDIDATES = 3

# ------------------------------------------------------------------
# 行业别名表：ETF 名称里常用的同义词。会在生成的 JSON 里落地，可手工补充。
# ------------------------------------------------------------------
DEFAULT_ALIASES = {
    # 科技
    "半导体": ["芯片", "集成电路", "科创芯片"],
    "电子": [],
    "消费电子": [],
    "元件": ["PCB", "电子元件"],
    "光学光电子": ["面板", "显示", "光电"],
    "计算机": ["信创", "云计算", "大数据"],
    "计算机设备": ["算力", "服务器"],
    "软件开发": ["软件", "信创", "人工智能"],
    "IT服务": ["软件", "计算机", "信创", "云计算"],
    "垂直应用软件": ["软件", "计算机"],
    "横向通用软件": ["软件", "计算机"],
    "数字芯片设计": ["芯片", "半导体"],
    "模拟芯片设计": ["芯片", "半导体"],
    "集成电路制造": ["芯片", "半导体", "集成电路"],
    "集成电路封测": ["芯片", "半导体", "集成电路"],
    "半导体设备": ["半导体", "芯片"],
    "半导体材料": ["半导体", "芯片材料"],
    "印制电路板": ["PCB", "电子元件"],
    "通信": ["5G"],
    "通信设备": ["光模块", "5G"],
    "通信网络设备及器件": ["通信", "5G", "光通信"],
    "通信服务": [],
    "传媒": ["游戏", "动漫", "影视", "文娱"],
    "影视院线": ["影视"],
    "互联网电商": ["电商", "互联网"],
    "教育": [],
    # 医药
    "医药生物": ["医药", "生物医药", "创新药"],
    "化学制药": ["制药", "创新药"],
    "化学制剂": ["制药", "创新药", "医药"],
    "中药": ["中药"],
    "医疗器械": [],
    "医疗服务": ["医疗"],
    "生物制品": ["疫苗", "生物技术"],
    "医药商业": [],
    # 金融地产
    "银行": [],
    "证券": ["券商"],
    "保险": ["保险", "非银"],
    "非银金融": ["证券", "券商", "保险", "非银"],
    "多元金融": ["金融"],
    "房地产": ["地产"],
    # 周期资源
    "煤炭": [],
    "钢铁": [],
    "石油石化": ["石油", "油气", "石化"],
    "工业金属": ["有色", "铜", "铝"],
    "铜": ["有色", "工业金属"],
    "铝": ["有色", "工业金属"],
    "小金属": ["稀土", "小金属"],
    "贵金属": ["黄金股", "贵金属"],
    "能源金属": ["稀有金属", "锂矿", "能源金属"],
    "金属新材料": ["新材料", "金属材料"],
    "基础化工": ["化工"],
    "化学制品": ["化工"],
    "化学原料": ["化工"],
    "化学纤维": ["化纤"],
    "农化制品": ["化肥", "农药", "农化"],
    # 装备制造
    "机械设备": ["机械"],
    "通用设备": [],
    "专用设备": [],
    "工程机械": [],
    "自动化设备": ["机器人", "自动化", "工业母机", "机床"],
    "工控设备": ["机器人", "自动化", "工业母机"],
    "电力设备": [],
    "电池": ["锂电", "电池"],
    "锂电池": ["锂电", "电池"],
    "电池化学品": ["锂电", "电池", "新能源"],
    "光伏设备": ["光伏"],
    "风电设备": ["风电"],
    "电网设备": ["电网", "特高压"],
    "汽车": ["汽车", "新能源车", "新能车", "智能车"],
    "汽车零部件": ["汽配", "汽车零部件"],
    "国防军工": ["军工", "国防", "航天"],
    # 消费
    "食品饮料": ["食品", "饮料", "白酒", "酒"],
    "白酒": ["白酒", "酒"],
    "家用电器": ["家电"],
    "白色家电": ["家电"],
    "黑色家电": ["家电"],
    "小家电": ["家电"],
    "厨卫电器": ["厨电", "家电"],
    "商贸零售": ["零售", "商贸"],
    "社会服务": ["旅游", "酒店", "餐饮"],
    "美容护理": ["医美", "化妆品", "美容"],
    "纺织服饰": ["服装", "纺织"],
    "纺织制造": ["纺织"],
    "服装家纺": ["服装", "家纺"],
    "家居用品": ["家居", "家具"],
    "轻工制造": ["轻工"],
    "包装印刷": [],
    "造纸": [],
    # 农业
    "农林牧渔": ["农业", "农牧"],
    "养殖业": ["养殖", "畜牧"],
    "农产品加工": ["农产品", "粮食"],
    # 公用/其他
    "电力": ["绿电", "电力"],
    "公用事业": ["公用"],
    "环保": [],
    "环境治理": ["环保", "环境"],
    "交通运输": ["交通", "运输", "航空", "航运"],
    "物流": ["快递", "物流"],
    "建筑材料": ["建材", "水泥", "玻璃"],
    "建筑装饰": ["建筑", "基建"],
    "综合": [],
    "其他": [],
}

LEVEL_PRIORITY = {"sw3": 300, "sw2": 200, "sw1": 100}
LEVEL_LABELS = {"sw3": "申万三级", "sw2": "申万二级回退", "sw1": "申万一级回退"}

# 规范名里含这些词 → 不是 A 股行业 ETF，排除
EXCLUDE_KEYWORDS = [
    # 宽基/规模/风格指数
    "沪深", "中证", "上证", "深证", "国证", "综指", "全指",
    "300", "500", "1000", "2000", "50", "100", "180", "A50", "A500",
    "创业板", "科创50", "科创100", "科创创业", "科创板",
    "红利", "低波", "基本面", "增强", "等权", "价值", "成长",
    "央企", "国企", "ESG", "治理",
    # 跨境
    "港股", "恒生", "H股", "香港", "中概", "纳指", "纳斯达克", "标普",
    "道琼", "日经", "德国", "法国", "美国", "印度", "东南亚", "亚太",
    "沙特", "全球", "海外",
    # 商品/债券/货币/其他资产
    "债", "货币", "现金", "白银", "豆粕", "原油", "能源化工", "饲料",
    "REIT",
]

# 新浪 ETF 简称常见的基金公司后缀（跟在 "ETF" 后面）
COMPANY_SUFFIXES = {
    "天弘", "国泰", "华夏", "易方达", "广发", "南方", "华泰柏瑞", "嘉实",
    "银华", "汇添富", "鹏华", "华宝", "博时", "富国", "招商", "工银",
    "建信", "平安", "大成", "华安", "景顺长城", "摩根", "国联安", "万家",
    "融通", "长盛", "中银", "诺安", "银河", "海富通", "申万菱信", "华富",
    "民生加银", "兴业", "永赢", "长城", "前海开源", "新华", "泰康",
    "中信保诚", "浦银安盛", "农银汇理", "交银施罗德", "东财", "西藏东财",
    "方正富邦", "汇安", "中金", "华泰保兴", "中华交易", "国寿安保",
    "上银", "鑫元", "弘毅远方", "兴业", "嘉合", "中航", "太平",
}


def _normalize_name(name: str) -> str:
    """ETF 简称规范化：去掉 ETF 字样和公司后缀，用于行业匹配。"""
    n = (name or "").strip()
    if "ETF" in n:
        base, _, desc = n.partition("ETF")
        n = base if (not desc or desc in COMPANY_SUFFIXES) else base + desc
    return n


def _normalize_industry(name: str) -> str:
    """去掉申万层级尾标，保留用于 ETF 名称匹配的行业主体。"""
    return (name or "").strip().rstrip("ⅠⅡⅢ")


def _is_excluded(norm_name: str) -> bool:
    if "黄金股" in norm_name:
        return False  # 黄金股ETF 是股票 ETF
    if "黄金" in norm_name:
        return True  # 黄金ETF 是商品 ETF
    return any(k in norm_name for k in EXCLUDE_KEYWORDS)


def _match_score(industry: str, keywords: list[str], norm_name: str) -> tuple[int, str] | None:
    """匹配质量评分，分数越高越贴近。"""
    if norm_name == industry:
        return (100, "exact")
    if industry in norm_name:
        return (90 - (len(norm_name) - len(industry)), "contain")
    for kw in keywords:
        if norm_name == kw:
            return (80, "alias_exact")
    for kw in keywords:
        if kw and kw in norm_name:
            return (70 - (len(norm_name) - len(kw)), "alias")
    return None


def _fetch_etf_list():
    """返回 [{code, name, turnover}]，code 为 6 位数字。"""
    try:
        df = ak.fund_etf_category_sina(symbol="ETF基金")
        rows = []
        for _, r in df.iterrows():
            raw_code = str(r.get("代码", ""))
            code = raw_code[-6:] if len(raw_code) >= 6 else raw_code
            try:
                turnover = float(r.get("成交额", 0) or 0)
            except (TypeError, ValueError):
                turnover = 0.0
            rows.append({"code": code, "name": str(r.get("名称", "")), "turnover": turnover})
        if rows:
            print(f"ETF 列表: 新浪 {len(rows)} 只")
            return rows
    except Exception as e:
        print(f"新浪 ETF 列表失败: {e}，回退东财...")
    df = ak.fund_etf_spot_em()
    rows = []
    for _, r in df.iterrows():
        try:
            turnover = float(r.get("成交额", 0) or 0)
        except (TypeError, ValueError):
            turnover = 0.0
        rows.append({"code": str(r.get("代码", "")), "name": str(r.get("名称", "")), "turnover": turnover})
    print(f"ETF 列表: 东财 {len(rows)} 只")
    return rows


def _sw3_industries() -> list[str]:
    db = get_db()
    rows = db.conn.execute(
        "SELECT DISTINCT industry FROM daily_new_highs WHERE scheme=? AND is_total=0",
        [SCHEME],
    ).fetchall()
    return sorted(r[0] for r in rows)


def _load_parent_map() -> dict[str, dict[str, str]]:
    """从个股申万分类缓存推导三级行业最常见的二级、一级父级。"""
    path = data_path(TAXONOMY_FILE)
    if not os.path.exists(path):
        path = resource_path(TAXONOMY_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        taxonomy = json.load(f)

    parents: dict[str, Counter] = defaultdict(Counter)
    for row in (taxonomy.get("stocks") or {}).values():
        sw1 = row.get("sw_level1") or ""
        sw2 = row.get("sw_level2") or ""
        sw3 = row.get("sw_level3") or sw2 or sw1
        if sw3:
            parents[sw3][(sw2 or sw1, sw1)] += 1
        if sw2:
            parents[sw2][(sw2, sw1)] += 1
        if sw1:
            parents[sw1][(sw1, sw1)] += 1

    result = {}
    for industry, counts in parents.items():
        (sw2, sw1), _ = counts.most_common(1)[0]
        result[industry] = {"sw2": sw2, "sw1": sw1}
    return result


def _candidate_targets(industry: str, parent_map: dict[str, dict[str, str]]):
    parent = parent_map.get(industry) or {}
    values = [("sw3", industry), ("sw2", parent.get("sw2")), ("sw1", parent.get("sw1"))]
    seen = set()
    for level, value in values:
        norm = _normalize_industry(value or "")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        yield level, value, norm


def _build_mapping(industries: list[str], etfs: list[dict], parent_map: dict[str, dict[str, str]],
                   aliases: dict[str, list[str]], overrides: dict) -> tuple[dict, list[str]]:
    """构建分层映射；同一 ETF 仅保留匹配层级最高的一次。"""
    pool = []
    for etf in etfs:
        norm = _normalize_name(etf["name"])
        if norm and not _is_excluded(norm):
            pool.append({**etf, "norm": norm})

    mapping, unmatched = {}, []
    for industry in industries:
        if industry in overrides:
            manual = []
            for candidate in overrides[industry]:
                item = dict(candidate)
                item.setdefault("match", "override")
                item.setdefault("match_level", "override")
                item.setdefault("matched_industry", industry)
                manual.append(item)
            mapping[industry] = manual
            if not manual:
                unmatched.append(f"{industry}(手工排除)")
            continue

        by_code = {}
        for level, source_name, target in _candidate_targets(industry, parent_map):
            keywords = []
            for key in (source_name, target):
                keywords.extend(aliases.get(key, []))
            keywords = list(dict.fromkeys(_normalize_industry(k) for k in keywords if k))
            for etf in pool:
                matched = _match_score(target, keywords, etf["norm"])
                if not matched:
                    continue
                score = LEVEL_PRIORITY[level] + matched[0]
                candidate = {
                    "code": etf["code"],
                    "name": etf["name"],
                    "match": matched[1],
                    "match_level": level,
                    "matched_industry": source_name,
                    "match_label": LEVEL_LABELS[level],
                    "_score": score,
                    "_turnover": etf.get("turnover", 0),
                }
                old = by_code.get(etf["code"])
                if old is None or (score, candidate["_turnover"]) > (old["_score"], old["_turnover"]):
                    by_code[etf["code"]] = candidate

        ranked = sorted(by_code.values(), key=lambda item: (-item["_score"], -item["_turnover"]))
        mapping[industry] = [
            {key: value for key, value in candidate.items() if not key.startswith("_")}
            for candidate in ranked[:MAX_CANDIDATES]
        ]
        if not mapping[industry]:
            unmatched.append(industry)
    return mapping, unmatched


def build_map() -> dict:
    industries = _sw3_industries()
    etfs = _fetch_etf_list()
    parent_map = _load_parent_map()

    # 读取旧配置，保留手工修改
    old = {}
    path = data_path(MAP_FILE)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, json.JSONDecodeError):
            old = {}
    aliases = dict(DEFAULT_ALIASES)
    for ind, extra in (old.get("aliases") or {}).items():
        aliases[ind] = extra  # 用户对该行业的别名整组替换
    overrides = old.get("overrides") or {}

    mapping, unmatched = _build_mapping(industries, etfs, parent_map, aliases, overrides)

    return {
        "version": 2,
        "scheme": SCHEME,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "aliases": aliases,
        "overrides": overrides,
        "mapping": mapping,
        "unmatched": unmatched,
        "parent_map": {industry: parent_map.get(industry, {}) for industry in industries},
    }


def main():
    result = build_map()
    path = data_path(MAP_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    matched = sum(1 for v in result["mapping"].values() if v)
    levels = Counter(v[0].get("match_level") for v in result["mapping"].values() if v)
    print(f"\n✅ 映射表已生成: {path}")
    print(f"   已匹配 {matched} 个行业，未匹配 {len(result['unmatched'])} 个:")
    print(f"   匹配层级: {dict(levels)}")
    for ind in result["unmatched"]:
        print(f"   - {ind}")
    return result


if __name__ == "__main__":
    main()
