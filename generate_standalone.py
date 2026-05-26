"""
生成独立 HTML 文件 — 将新高 JSON 数据内嵌到 industry-heatmap.html 中，
无需服务器即可在手机浏览器中直接打开查看。
"""

import json
import os

STATIC = os.path.join(os.path.dirname(__file__), "static")
TYPES = ["month", "60d", "120d", "1year", "alltime"]


def read_json(filename):
    with open(os.path.join(STATIC, filename), "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    # 读取所有 counts 和 details
    all_counts = {}
    all_details = {}
    for t in TYPES:
        all_counts[t] = read_json(f"new_highs_data_{t}.json")
        all_details[t] = read_json(f"new_highs_details_{t}.json")
        print(f"  [{t}] counts: {len(json.dumps(all_counts[t])):,} bytes, "
              f"details: {len(json.dumps(all_details[t])):,} bytes")

    # 读取 HTML 模板
    template_path = os.path.join(STATIC, "industry-heatmap.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    # 构建内嵌数据脚本（插入在 SheetJS CDN 之后）
    embed_js = "\n<script>\n// === 内嵌数据（免服务器） ===\n"
    embed_js += "var EMBEDDED_COUNTS = {\n"
    for t in TYPES:
        embed_js += f'  "{t}": {json.dumps(all_counts[t], ensure_ascii=False)},\n'
    embed_js += "};\n"
    embed_js += "var EMBEDDED_DETAILS = {\n"
    for t in TYPES:
        embed_js += f'  "{t}": {json.dumps(all_details[t], ensure_ascii=False)},\n'
    embed_js += "};\n"
    embed_js += "</script>\n"

    # 插入到 SheetJS script 之后
    sheetjs_tag = '<script src="https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js"></script>'
    html = html.replace(sheetjs_tag, sheetjs_tag + "\n" + embed_js)

    # 修改 getUrls() — 独立模式下不需要
    html = html.replace(
        "function getUrls() {\n  var suffix = currentType;\n  return {\n    counts: '/new_highs_data_' + suffix + '.json',\n    details: '/new_highs_details_' + suffix + '.json',\n  };\n}",
        "// 独立模式：数据已内嵌，无需 URL"
    )

    # 修改 loadData() — 从 EMBEDDED_COUNTS 读取
    html = html.replace(
        "async function loadData() {\n  var mainContent = document.getElementById('mainContent');\n  mainContent.innerHTML = '<div class=\"loading\">正在加载数据...</div>';\n  try {\n    var urls = getUrls();\n    var resp = await fetch(urls.counts);\n    if (!resp.ok) throw new Error('HTTP ' + resp.status);\n    tableData = await resp.json();\n    renderTable();\n    setupExportButton();\n    document.getElementById('updateNote').textContent =\n      '数据更新于 ' + (tableData.updated_at || 'unknown') + ' · 数据源: 同花顺/申万研究所 · 点击单元格查看个股明细';\n  } catch (e) {\n    mainContent.innerHTML =\n      '<div class=\"error-msg\">' +\n      '数据加载失败: ' + e.message +\n      '<br><button onclick=\"location.reload()\">重试</button>' +\n      '<br><br><span style=\"font-size:12px;color:var(--text-dim)\">请先运行: python fetch_new_highs.py --type ' + currentType + '</span>' +\n      '</div>';\n  }\n}",
        "function loadData() {\n  var mainContent = document.getElementById('mainContent');\n  tableData = EMBEDDED_COUNTS[currentType];\n  if (!tableData || !tableData.industries) {\n    mainContent.innerHTML = '<div class=\"error-msg\">数据不可用，请重新生成 HTML</div>';\n    return;\n  }\n  renderTable();\n  setupExportButton();\n  document.getElementById('updateNote').textContent =\n    '数据更新于 ' + (tableData.updated_at || 'unknown') + ' · 数据源: 同花顺/申万研究所 · 点击单元格查看个股明细';\n}"
    )

    # 修改 ensureDetails() — 从 EMBEDDED_DETAILS 读取
    html = html.replace(
        "async function ensureDetails() {\n  if (detailsData && detailsLoadedType === currentType) return detailsData;\n  if (detailsLoading) {\n    while (detailsLoading) await new Promise(function(r) { setTimeout(r, 100); });\n    if (detailsData && detailsLoadedType === currentType) return detailsData;\n  }\n  detailsLoading = true;\n  try {\n    var urls = getUrls();\n    var resp = await fetch(urls.details);\n    if (!resp.ok) throw new Error('HTTP ' + resp.status);\n    detailsData = await resp.json();\n    detailsLoadedType = currentType;\n    return detailsData;\n  } catch (e) {\n    console.error('加载明细失败:', e);\n    return null;\n  } finally {\n    detailsLoading = false;\n  }\n}",
        "function ensureDetails() {\n  if (detailsData && detailsLoadedType === currentType) return detailsData;\n  detailsData = EMBEDDED_DETAILS[currentType] || null;\n  detailsLoadedType = currentType;\n  return detailsData;\n}"
    )

    # 移除 async/await — ensureDetails 改为同步
    html = html.replace(
        "async function openDetail(industry, dateLabel, count) {",
        "function openDetail(industry, dateLabel, count) {"
    )
    html = html.replace(
        "async function openTotalDetail(dateLabel, count) {",
        "function openTotalDetail(dateLabel, count) {"
    )
    html = html.replace(
        "async function exportToExcel(dateLabel) {",
        "function exportToExcel(dateLabel) {"
    )
    html = html.replace("var details = await ensureDetails();", "var details = ensureDetails();")

    # 保存
    output_path = os.path.join(STATIC, "industry-heatmap-standalone.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\n已生成: {output_path}")
    print(f"文件大小: {size_kb:,.0f} KB")


if __name__ == "__main__":
    main()
