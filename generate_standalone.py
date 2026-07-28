"""生成可离线打开的行业机会热力图收盘快照。

独立版复用正式页面，不维护第二套 UI。生成时把三套行业分类、五个
观察窗口、个股明细和机会状态一起内嵌，并用只读 fetch 适配器提供给页面。
盘中扫描和自定义窗口需要本地服务，因此在独立版中明确禁用。
"""

from __future__ import annotations

import json
import os
from typing import Any

from heatmap_opportunity import load_opportunity_snapshot
from runtime_paths import DATA_DIR, RESOURCE_STATIC_DIR


PERIODS = ("month", "60d", "120d", "1year", "alltime")
SCHEMES = {"sw": "", "ths": "_ths", "sw3": "_sw3"}
DATA_PREFIXES = (
    "new_highs_data",
    "new_lows_data",
    "new_highs_details",
    "new_lows_details",
)
# 明细弹窗的辅助数据：多周期新高/新低计数与市盈率。
# 不分分类/窗口，各自单文件内嵌（合计约 170KB，体积可接受），
# 否则独立版弹窗中多周期计数与 PE 列恒为 0 且无任何提示。
EXTRA_FILES = (
    "highs_period_counts.json",
    "lows_period_counts.json",
    "stock_pe.json",
)


def _read_json(filename: str) -> Any:
    path = os.path.join(DATA_DIR, filename)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _json_for_script(value: Any) -> str:
    # 内嵌进 <script> 的 JSON 不能出现 "</"（提前闭合脚本）或 "<!--"（进入注释状态）
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("</", "<\\/")
        .replace("<!--", "<\\!--")
    )


def _embedded_payloads() -> tuple[dict[str, Any], dict[str, Any]]:
    assets: dict[str, Any] = {}
    opportunities: dict[str, Any] = {}
    for scheme, suffix in SCHEMES.items():
        for period in PERIODS:
            for prefix in DATA_PREFIXES:
                filename = f"{prefix}_{period}{suffix}.json"
                payload = _read_json(filename)
                if payload is not None:
                    assets[f"/{filename}"] = payload
            try:
                opportunities[f"{scheme}|{period}"] = load_opportunity_snapshot(
                    DATA_DIR,
                    scheme=scheme,
                    period=period,
                    mode="daily",
                )
            except (FileNotFoundError, ValueError):
                continue
        flow_name = f"capital_flow{suffix}.json"
        flow = _read_json(flow_name)
        if flow is not None:
            assets[f"/{flow_name}"] = flow
    for filename in EXTRA_FILES:
        payload = _read_json(filename)
        if payload is not None:
            assets[f"/{filename}"] = payload
    return assets, opportunities


def _fetch_adapter(assets: dict[str, Any], opportunities: dict[str, Any]) -> str:
    return """
<script>
// === 独立收盘快照：内嵌数据与只读请求适配器 ===
window.STANDALONE_MODE = true;
var STANDALONE_ASSETS = %s;
var STANDALONE_OPPORTUNITIES = %s;
function standaloneResponse(payload, status) {
  var code = status || 200;
  return Promise.resolve({
    ok: code >= 200 && code < 300,
    status: code,
    json: function() { return Promise.resolve(payload); },
    text: function() { return Promise.resolve(JSON.stringify(payload)); }
  });
}
window.fetch = function(input) {
  var raw = typeof input === 'string' ? input : (input && input.url) || '';
  var parsed = new URL(raw, 'http://standalone.local/');
  if (parsed.pathname === '/api/market-session') {
    return standaloneResponse({
      phase: 'closed',
      recommended_mode: 'daily',
      message: '独立版为生成时的收盘快照'
    });
  }
  if (parsed.pathname === '/api/heatmap-opportunities') {
    var scheme = parsed.searchParams.get('scheme') || 'sw';
    var period = parsed.searchParams.get('period') || 'month';
    var opportunity = STANDALONE_OPPORTUNITIES[scheme + '|' + period];
    return opportunity
      ? standaloneResponse(opportunity)
      : standaloneResponse({detail: '该分类或窗口未内嵌'}, 404);
  }
  if (Object.prototype.hasOwnProperty.call(STANDALONE_ASSETS, parsed.pathname)) {
    return standaloneResponse(STANDALONE_ASSETS[parsed.pathname]);
  }
  return standaloneResponse({detail: '独立版不提供此实时接口'}, 404);
};
</script>
""" % (_json_for_script(assets), _json_for_script(opportunities))


def _replace_required(html: str, old: str, new: str, count: int = -1) -> str:
    """精确替换并要求命中；模板文案变化导致未命中时直接报错退出，
    避免静默生成出与预期不符的独立版。"""
    if old not in html:
        raise SystemExit(f"模板替换未命中，请检查 industry-heatmap.html 是否已改动: {old!r}")
    return html.replace(old, new, count)


def main() -> None:
    template_path = os.path.join(RESOURCE_STATIC_DIR, "industry-heatmap.html")
    with open(template_path, encoding="utf-8") as handle:
        html = handle.read()
    guide_path = os.path.join(RESOURCE_STATIC_DIR, "page-guide.js")
    with open(guide_path, encoding="utf-8") as handle:
        guide_script = handle.read()
    html = _replace_required(
        html,
        '<script src="/page-guide.js" defer></script>',
        "<script>\n" + guide_script + "\n</script>",
    )

    assets, opportunities = _embedded_payloads()
    html = _replace_required(
        html, "</head>", _fetch_adapter(assets, opportunities) + "\n</head>", 1
    )
    html = _replace_required(
        html,
        '<button class="toggle-btn active" data-mode="auto">自动</button>',
        '<button class="toggle-btn active" data-mode="auto">独立收盘快照</button>',
    )
    html = _replace_required(
        html,
        '<button class="toggle-btn" data-mode="intraday">📡 盘中实时</button>',
        "",
    )
    html = _replace_required(
        html,
        '<button class="toggle-btn" data-type="custom">自定义</button>',
        "",
    )
    html = _replace_required(
        html,
        '<label class="custom-window-control" title="自定义回看交易日，范围5至250日">',
        '<label class="custom-window-control" style="display:none" '
        'title="独立版不提供自定义窗口">',
    )

    output_path = os.path.join(DATA_DIR, "industry-heatmap-standalone.html")
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(html)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(
        f"已生成: {output_path}\n"
        f"文件大小: {size_mb:,.1f} MB · "
        f"数据文件 {len(assets)} 个 · 机会快照 {len(opportunities)} 个"
    )


if __name__ == "__main__":
    main()
