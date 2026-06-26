"""
生成独立 HTML 文件 — 将新高/新低 JSON 数据内嵌到 industry-heatmap.html 中，
无需服务器即可在手机浏览器中直接打开查看。
"""

import json
import os

STATIC = os.path.join(os.path.dirname(__file__), "static")
TYPES = ["month", "60d", "120d", "1year", "alltime"]


def read_json(filename):
    path = os.path.join(STATIC, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def main():
    # 读取所有 counts 和 details（新高 + 新低）
    all_counts = {"highs": {}, "lows": {}}
    all_details = {"highs": {}, "lows": {}}
    for t in TYPES:
        all_counts["highs"][t] = read_json(f"new_highs_data_{t}.json")
        all_details["highs"][t] = read_json(f"new_highs_details_{t}.json")
        print(f"  [highs/{t}] counts: {len(json.dumps(all_counts['highs'][t])):,} bytes, "
              f"details: {len(json.dumps(all_details['highs'][t])):,} bytes")
    for t in TYPES:
        all_counts["lows"][t] = read_json(f"new_lows_data_{t}.json")
        all_details["lows"][t] = read_json(f"new_lows_details_{t}.json")
        if all_counts["lows"][t]:
            print(f"  [lows/{t}]  counts: {len(json.dumps(all_counts['lows'][t])):,} bytes, "
                  f"details: {len(json.dumps(all_details['lows'][t])):,} bytes")

    # 读取 HTML 模板
    template_path = os.path.join(STATIC, "industry-heatmap.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    # 内嵌 xlsx 库（独立版不依赖 CDN）
    xlsx_path = os.path.join(STATIC, "xlsx.full.min.js")
    with open(xlsx_path, "r", encoding="utf-8") as f:
        xlsx_js = f.read()
    # 替换外部引用为内嵌脚本
    sheetjs_tag = '<script src="/xlsx.full.min.js"></script>'
    html = html.replace(sheetjs_tag, '<script>' + xlsx_js + '</script>')

    # 构建内嵌数据脚本
    embed_js = "\n<script>\n// === 内嵌数据（免服务器） ===\n"
    embed_js += "var EMBEDDED_COUNTS = {\n"
    embed_js += '  "highs": {\n'
    for t in TYPES:
        embed_js += f'    "{t}": {json.dumps(all_counts["highs"][t], ensure_ascii=False)},\n'
    embed_js += '  },\n'
    embed_js += '  "lows": {\n'
    for t in TYPES:
        if all_counts["lows"][t]:
            embed_js += f'    "{t}": {json.dumps(all_counts["lows"][t], ensure_ascii=False)},\n'
    embed_js += '  }\n'
    embed_js += "};\n"
    embed_js += "var EMBEDDED_DETAILS = {\n"
    embed_js += '  "highs": {\n'
    for t in TYPES:
        embed_js += f'    "{t}": {json.dumps(all_details["highs"][t], ensure_ascii=False)},\n'
    embed_js += '  },\n'
    embed_js += '  "lows": {\n'
    for t in TYPES:
        if all_details["lows"][t]:
            embed_js += f'    "{t}": {json.dumps(all_details["lows"][t], ensure_ascii=False)},\n'
    embed_js += '  }\n'
    embed_js += "};\n"
    embed_js += "</script>\n"

    # 插入到 SheetJS 内嵌脚本之后
    html = html.replace('</script>', '</script>\n' + embed_js, 1)

    # 修改 getUrls() — 独立模式下不需要
    # 匹配更新后的 getUrls（包含 direction 判断）
    old_get_urls = ("function getUrls() {\n"
                    "  var suffix = currentType;\n"
                    "  var prefix = currentDirection === 'lows' ? 'new_lows' : 'new_highs';\n"
                    "  return {\n"
                    "    counts: '/' + prefix + '_data_' + suffix + '.json',\n"
                    "    details: '/' + prefix + '_details_' + suffix + '.json',\n"
                    "  };\n}")
    html = html.replace(old_get_urls, "// 独立模式：数据已内嵌，无需 URL")

    # 修改 loadData() — 从 EMBEDDED_COUNTS 读取
    old_load_data = (
        "async function loadData() {\n"
        "  var mainContent = document.getElementById('mainContent');\n"
        "  mainContent.innerHTML = '<div class=\"loading\">正在加载数据...</div>';\n"
        "  try {\n"
        "    var urls = getUrls();\n"
        "    var resp = await fetch(urls.counts);\n"
        "    if (!resp.ok) throw new Error('HTTP ' + resp.status);\n"
        "    tableData = await resp.json();\n"
        "    updateLabels();\n"
        "    renderTable();\n"
        "    setupExportButton();\n"
        "    document.getElementById('updateNote').textContent =\n"
        "      '数据更新于 ' + (tableData.updated_at || 'unknown') + ' · 数据源: 同花顺/申万研究所 · 点击单元格查看个股明细';\n"
        "  } catch (e) {\n"
        "    var scriptName = currentDirection === 'lows' ? 'fetch_new_lows.py' : 'fetch_new_highs.py';\n"
        "    mainContent.innerHTML =\n"
        "      '<div class=\"error-msg\">' +\n"
        "      '数据加载失败: ' + e.message +\n"
        "      '<br><button onclick=\"location.reload()\">重试</button>' +\n"
        "      '<br><br><span style=\"font-size:12px;color:var(--text-dim)\">请先运行: python ' + scriptName + ' --type ' + currentType + '</span>' +\n"
        "      '</div>';\n"
        "  }\n"
        "}")
    new_load_data = (
        "function loadData() {\n"
        "  var mainContent = document.getElementById('mainContent');\n"
        "  var dirData = EMBEDDED_COUNTS[currentDirection];\n"
        "  if (!dirData) { mainContent.innerHTML = '<div class=\"error-msg\">方向数据不可用</div>'; return; }\n"
        "  tableData = dirData[currentType];\n"
        "  if (!tableData || !tableData.industries) {\n"
        "    mainContent.innerHTML = '<div class=\"error-msg\">数据不可用，请重新生成 HTML</div>';\n"
        "    return;\n"
        "  }\n"
        "  updateLabels();\n"
        "  renderTable();\n"
        "  setupExportButton();\n"
        "  document.getElementById('updateNote').textContent =\n"
        "    '数据更新于 ' + (tableData.updated_at || 'unknown') + ' · 数据源: 同花顺/申万研究所 · 点击单元格查看个股明细';\n"
        "}")
    html = html.replace(old_load_data, new_load_data)

    # 修改 ensureDetails() — 从 EMBEDDED_DETAILS 读取
    old_ensure_details = (
        "async function ensureDetails() {\n"
        "  if (detailsData && detailsLoadedType === currentType && detailsLoadedDirection === currentDirection) return detailsData;\n"
        "  if (detailsLoading) {\n"
        "    while (detailsLoading) await new Promise(function(r) { setTimeout(r, 100); });\n"
        "    if (detailsData && detailsLoadedType === currentType && detailsLoadedDirection === currentDirection) return detailsData;\n"
        "  }\n"
        "  detailsLoading = true;\n"
        "  try {\n"
        "    var urls = getUrls();\n"
        "    var resp = await fetch(urls.details);\n"
        "    if (!resp.ok) throw new Error('HTTP ' + resp.status);\n"
        "    detailsData = await resp.json();\n"
        "    detailsLoadedType = currentType;\n"
        "    detailsLoadedDirection = currentDirection;\n"
        "    return detailsData;\n"
        "  } catch (e) {\n"
        "    console.error('加载明细失败:', e);\n"
        "    return null;\n"
        "  } finally {\n"
        "    detailsLoading = false;\n"
        "  }\n"
        "}")
    new_ensure_details = (
        "function ensureDetails() {\n"
        "  if (detailsData && detailsLoadedType === currentType && detailsLoadedDirection === currentDirection) return detailsData;\n"
        "  var dirData = EMBEDDED_DETAILS[currentDirection];\n"
        "  detailsData = (dirData && dirData[currentType]) ? dirData[currentType] : null;\n"
        "  detailsLoadedType = currentType;\n"
        "  detailsLoadedDirection = currentDirection;\n"
        "  return detailsData;\n"
        "}")
    html = html.replace(old_ensure_details, new_ensure_details)

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
