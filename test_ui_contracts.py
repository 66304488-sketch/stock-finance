import subprocess
import unittest
from pathlib import Path


STATIC = Path(__file__).with_name("static")


class UiContractTest(unittest.TestCase):
    def test_decision_center_closes_the_nine_item_loop(self):
        app = (STATIC / "app.html").read_text(encoding="utf-8")
        page = (STATIC / "decision-center.html").read_text(encoding="utf-8")
        server = Path("server.py").read_text(encoding="utf-8")
        self.assertIn('data-tab="decision"', app)
        self.assertIn('src="/decision-center.html"', app)
        for endpoint in (
            '@app.get("/api/decision-center")',
            '@app.get("/api/decision-center/industry")',
            '@app.get("/api/decision-center/portfolio")',
        ):
            self.assertIn(endpoint, server)
        for text in (
            "市场环境总开关",
            "当前值、一阶变化和二阶变化",
            "驱动原因：证据归因与事件日志",
            "板块内部结构与传导",
            "可交易性与成本",
            "触发、确认、失效与风险升级",
            "组合风险",
            "统一提醒与冷却",
            "概率可信度",
            "decision-alert-acks-v1",
            "decision-events-v1",
            "/api/decision-center/industry?",
            "/api/decision-center/portfolio?",
        ):
            self.assertIn(text, page)

    def test_decision_center_boots_with_mocked_payload(self):
        script = r"""
const fs = require('fs');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');
const html = fs.readFileSync('static/decision-center.html', 'utf8');
const card = {
  industry:'测试行业', lane:'opportunity', attention_score:72, summary:'首次转强，等待确认', source_date:'20260106',
  change:{label:'首次转强',state:'first_strength',flags:['first_strength'],evidence:['价格转正'],metrics:{price:{value:1,delta:2,acceleration:1},breadth:{value:30,delta:20,acceleration:10},direction:{value:20,delta:15,acceleration:8},activity:{value:60,delta:5,acceleration:2}}},
  drivers:{primary:{label:'方向成交',direction:'positive',strength:70,evidence:'方向成交改善'},agreement_count:2,event_source:{label:'事件源未自动接入',note:'人工记录'},domains:[{label:'方向成交',direction:'positive',strength:70,evidence:'改善',available:true}]},
  structure:{label:'龙头扩散',state:'leader_diffusion',advance_ratio_pct:65,top5_amount_share_pct:50,effective_participation_pct:12,leader_persistence_5d:.5,leader_labels:['龙头A'],leaders:[{name:'龙头A',code:'000001'}]},
  tradability:{mode_label:'T+1确认',mode:'next_open_confirm',can_trade:true,liquidity_score:80,estimated_cost_bps:8,cost_kind:'估算',carrier:{name:'测试ETF',code:'510001',avg_amount_20d:5e8},microstructure:{note:'盘中确认'}},
  probability:{label:'研究样本',available:true,samples:200,independent_dates:80,ci_t5:[45,60],trust_score:65,horizons:[],reason:'历史频率',precision_note:'不伪造'},
  trade_plan:{signal_time:'收盘',horizon:'T1-T5',trigger:'等待',confirmation:'两项确认',action:'观察',invalidation:['转弱'],risk_upgrade:'风险升级',max_risk:'1R',position_band:'观察仓',danger_level:'normal'},
  transmission:{nodes:[{id:'driver',type:'driver',label:'方向成交',status:'positive'},{id:'industry',type:'industry',label:'测试行业',status:'leader_diffusion'}],edges:[{from:'driver',to:'industry',label:'数据归因'}]},
  radar:{sentiment_label:'中性',danger:{label:'正常'}}, opportunity:{confirmation_count:2,conflicts:[],risks:[],vetoes:[]}, alerts:[]
};
const payload = {
  as_of:'20260106', quality:{status:'valid',label:'可决策',as_of:'20260106',warnings:[],sources:{capital_flow:{date:'20260106',status:'aligned'}}},
  regime:{label:'健康轮动',permission:'allowed',reason:'宽度改善',metrics:{market_return_pct:1,breadth_pct:50,breadth_change_pp:10,activity_percentile:70,amount_ratio_20:1.2,style_spread_pct:1,temperature:60},style:{groups:[]},indices:[],strategy_fit:{breakout:60,reversal:50,rotation:80,defense:20},risks:[],evidence:['宽度改善']},
  battle_cards:[card], catalog:[{industry:'测试行业',change_label:'首次转强',structure_label:'龙头扩散'}], alerts:[], counts:{industries:1}
};
const errors=[];const virtualConsole=new VirtualConsole();virtualConsole.on('jsdomError',e=>errors.push(e));
const dom=new JSDOM(html,{runScripts:'dangerously',pretendToBeVisual:true,url:'http://localhost/decision-center.html',virtualConsole,beforeParse(window){window.fetch=async()=>({ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(payload))});window.alert=()=>{};}});
setTimeout(()=>{
  assert.deepStrictEqual(errors.map(e=>e.message),[]);
  assert.ok(dom.window.document.querySelector('#battleCards').textContent.includes('测试行业'));
  assert.ok(dom.window.document.querySelector('#detailGrid').textContent.includes('概率可信度'));
  console.log('ok');
},80);
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_sentiment_radar_is_a_separate_dual_probability_page(self):
        app = (STATIC / "app.html").read_text(encoding="utf-8")
        page = (STATIC / "sentiment-radar.html").read_text(encoding="utf-8")
        guide = (STATIC / "page-guide.js").read_text(encoding="utf-8")
        self.assertIn('data-tab="radar"', app)
        self.assertIn('src="/sentiment-radar.html"', app)
        for text in (
            "板块情绪雷达", "底部反弹", "顶部退潮", "T1—T5历史条件频率",
            "支持证据与反对证据", "/api/sentiment-radar?scheme=",
            "板块个股", "/api/sentiment-radar/stocks?", "stockSearch",
            "成交影响", "stockpage.10jqka.com.cn",
        ):
            self.assertIn(text, page)
        self.assertIn("'sentiment-radar.html'", guide)
        self.assertIn("申万三级按有效成分数以κ=20", guide)
        self.assertIn("点击查看成分股", guide)
        self.assertIn("个股行情严格截断到雷达信号日", guide)

    def test_strategy_restores_real_controls_without_removed_ids(self):
        source = (STATIC / "app.html").read_text(encoding="utf-8")
        for removed in ("sourceHighs", "sourceLows", "sourceFlow", "sourceBasics",
                        "freezeHighs", "freezeLows", "freezeFlow"):
            self.assertNotIn(removed, source)
        self.assertIn("selected_datasets: getSelectedDatasets()", source)
        self.assertIn("refresh_days: getRefreshDays()", source)

    def test_all_batch_refresh_entry_points_honor_the_saved_dataset_strategy(self):
        app = (STATIC / "app.html").read_text(encoding="utf-8")
        status = (STATIC / "data-status.js").read_text(encoding="utf-8")
        server = Path("server.py").read_text(encoding="utf-8")
        electron = Path("electron/main.ts").read_text(encoding="utf-8")

        self.assertIn('value="market_cap" checked', app)
        self.assertIn("body: JSON.stringify({days:1})", app)
        self.assertNotIn(
            "datasets:['highs','lows','capital_flow','margin_financing']",
            app,
        )
        self.assertIn("var mcapFrame = document.querySelector('#panel-mcap iframe')", app)
        self.assertIn("fetchJsonChecked('/api/update-config')", app)
        self.assertIn("configured.filter(function(k){ return !!states[k]; })", app)

        self.assertIn("def _resolve_refresh_datasets", server)
        self.assertIn('strategy.get("selected_datasets")', server)
        self.assertIn('strategy.get("refresh_days", 1)', server)
        self.assertIn('strategy.get("update_mode", "auto")', server)
        self.assertIn('"etf", "market_cap"', server)

        self.assertIn('getBackendJson("/api/update-config")', electron)
        self.assertIn('"margin_financing", "market_cap"', electron)
        self.assertIn("function postRefresh(days?: number)", electron)
        self.assertNotIn('JSON.stringify({ days, mode: "auto" })', electron)

        self.assertIn("var monitoredDatasets = CORE_DATASETS.slice()", status)
        self.assertIn("strategy.config.selected_datasets", status)
        self.assertNotIn("datasets: CORE_DATASETS", status)

    def test_capital_flow_v2_adapter_preserves_legacy_newest_first_history(self):
        source = (STATIC / "capital-flow.html").read_text(encoding="utf-8")
        self.assertIn("daily.slice(dateIdx+1,dateIdx+61)", source)
        self.assertIn("var latest = flowData.dates && flowData.dates[0]", source)
        self.assertIn("'daily_stock_counts','daily_eligible_stocks'", source)
        self.assertIn("旧版方向成交额为涨跌方向加权的日线代理", source)
        self.assertIn("不是逐笔主动买卖、主力净流入或真实资金流向", source)

    def test_market_cap_v2_structure_attribution_and_legacy_adapter(self):
        source = (STATIC / "market-cap.html").read_text(encoding="utf-8")
        for text in ("市值结构与行情归因", "全市场市值变化归因", "行业总市值变化贡献 bp 瀑布", "规模—广度罗盘",
                     "HHI / 有效行业数", "大盘 Top100", "总市值变化贡献 bp",
                     "权重迁移 bp", "CW 收益", "EW 收益", "固定色标树图",
                     "20–60 日轨迹", "Price / Supply / Universe"):
            self.assertIn(text, source)
        self.assertIn("row.stocks_by_date||{}", source)
        self.assertIn("function legacyWindowReturn", source)
        self.assertIn("row.latest&&typeof row.latest==='object'", source)
        self.assertIn("Array.isArray(row.series)", source)
        self.assertIn("TREE_LIMITS={1:5,5:12,20:25}", source)
        self.assertNotIn("maxAbsChg", source)

    def test_market_cap_compass_can_expand_zoom_pan_and_reset(self):
        source = (STATIC / "market-cap.html").read_text(encoding="utf-8")
        for text in (
            'id="compassPanel"',
            'id="compassZoomOut"',
            'id="compassZoomIn"',
            'id="compassReset"',
            'id="compassExpand"',
            '.compass-panel.compass-expanded',
            'var compassView={scale:1,centerX:0,centerY:0,baseDomain:2}',
            'function zoomCompassAt(multiplier,anchorX,anchorY)',
            "wrap.addEventListener('wheel'",
            "wrap.addEventListener('pointerdown'",
            "wrap.addEventListener('pointermove'",
            'function resetCompassView(shouldRender)',
            'fill="transparent" stroke="none" pointer-events="all"',
            'hitR=Math.max(r+5,12)',
            'resetCompassView(false)',
            '支持滚轮缩放和拖拽平移',
        ):
            self.assertIn(text, source)
        self.assertIn('aria-pressed="false"', source)
        guide = (STATIC / "page-guide.js").read_text(encoding="utf-8")
        self.assertIn("罗盘放大操作", guide)
        self.assertIn("小行业会绘制在大气泡上层", guide)

    def test_market_cap_treemap_labels_stay_crisp_in_light_and_dark_themes(self):
        source = (STATIC / "market-cap.html").read_text(encoding="utf-8")
        for text in (
            "color:#f8fafc",
            "text-shadow:none",
            "-webkit-font-smoothing:antialiased",
            ".tile-name { color:#fff",
            ".tile-value,.tile-return { color:#f2f6fb; opacity:.96",
        ):
            self.assertIn(text, source)
        self.assertNotIn("text-shadow:0 1px 3px #000b", source)
        self.assertNotIn("color:var(--c-f2f5f9", source)

    def test_market_cap_numeric_correctness_contracts_in_jsdom(self):
        script = r"""
const fs = require('fs');
const assert = require('assert');
const {JSDOM, VirtualConsole} = require('jsdom');
const html = fs.readFileSync('static/market-cap.html', 'utf8');

const dates = Array.from({length: 5}, (_, i) => {
  const day = String(i + 1).padStart(2, '0');
  return {date: `2026-01-${day}`, label: `1月${i + 1}日`};
});
const marketSeries = dates.map((date, i) => ({
  date: date.date,
  total_mcap: 100 + i,
  market_return_pct: i === 4 ? 10 : 0.2,
  cap_weighted_return_pct: i === 4 ? 1 : 0.1,
  equal_weight_return_pct: i === 4 ? 0.5 : 0.1,
  stock_breadth_pct: i === 4 ? 60 : 50,
  industry_breadth_pct: 100,
  price_effect_bp: i === 4 ? 99 : 1,
  share_snapshot_effect_bp: i === 4 ? 77 : 1
}));
const industrySeries = dates.map((date, i) => ({
  date: date.date,
  mcap: 50 + i,
  weight_pct: 50,
  cap_weighted_return_pct: i === 4 ? 2 : 0.1,
  equal_weight_return_pct: i === 4 ? -1 : 0.1,
  stock_breadth_pct: i === 4 ? 60 : 50,
  contribution_bp: i === 4 ? 10 : 1,
  top5_stock_share_pct: 40,
  effective_stocks: 8
}));
const payload = {
  schema_version: 2,
  model_version: 'market-cap-structure-v2',
  scheme: 'sw',
  scheme_label: '申万一级',
  trade_date: '2026-01-05',
  dates,
  market: {
    latest: Object.assign({}, marketSeries[4], {
      return_1d_pct: 10,
      cap_weighted_return_1d_pct: 1,
      equal_weight_return_1d_pct: 0.5,
      stock_breadth_pct: 60,
      price_effect_bp: 99
    }),
    series: marketSeries
  },
  industries: [{
    industry: '行业甲',
    latest: Object.assign({}, industrySeries[4], {
      cap_weighted_return_1d_pct: 2,
      equal_weight_return_1d_pct: -1,
      stock_breadth_1d_pct: 60,
      contribution_1d_bp: 10
    }),
    series: industrySeries,
    top_stocks: [{name: '最新股票', code: '000001', mcap: 20, change_pct: 3}]
  }],
  data_quality: {
    measure_kind: 'point_in_time_total_mcap',
    history_days: 5,
    classification: {direct_ratio: 0.8, direct: 8, fallback: 2},
    point_in_time_shares: {available: true, coverage: 1, source: 'CNINFO', share_snapshot: '2026-01-05'},
    circulating_share_proxy: {available: false}
  }
};

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}
const secondIndustry = clone(payload.industries[0]);
secondIndustry.industry = '行业乙';
payload.industries.push(secondIndustry);
async function boot(data) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  virtualConsole.on('error', error => errors.push(error));
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost/market-cap.html',
    virtualConsole,
    beforeParse(window) {
      window.fetch = async () => ({ok: true, status: 200, json: async () => clone(data)});
      window.requestAnimationFrame = callback => {
        callback();
        return 1;
      };
    }
  });
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.deepStrictEqual(errors.map(error => error.message), []);
  return dom;
}
function chip(window, label) {
  return Array.from(window.document.querySelectorAll('#hero .evidence-chip')).find(
    item => item.querySelector('small').textContent.includes(label)
  );
}

(async () => {
  const dom = await boot(payload);
  const w = dom.window;
  const d = w.document;

  assert.strictEqual(chip(w, '总市值变化').querySelector('strong').textContent, '+10.00%');
  assert.strictEqual(chip(w, 'CW价格收益').querySelector('strong').textContent, '+1.00%');
  assert.strictEqual(chip(w, '个股上涨占比').querySelector('strong').textContent, '60%');
  assert.ok(!d.querySelector('#hero').textContent.includes('+60%'));
  assert.strictEqual(w.marketMetric(w.currentMetrics()).shareSnapshotEffect, 77);
  assert.ok(d.querySelector('#marketAttributionBody').textContent.includes('Price 价格效应'));
  assert.ok(d.querySelector('#marketAttributionBody').textContent.includes('+99.0 bp'));
  assert.ok(d.querySelector('#marketAttributionBody').textContent.includes('+77.0 bp'));
  assert.strictEqual(d.querySelector('#marketAttributionStatus').textContent, '部分归因');

  const compassHit = d.querySelector('#compassWrap [data-industry] circle[pointer-events="all"]');
  assert.ok(compassHit);
  assert.ok(Number(compassHit.getAttribute('r')) >= 12);
  assert.strictEqual(d.querySelector('#compassZoomLevel').textContent, '100%');
  d.querySelector('#compassZoomIn').click();
  assert.strictEqual(d.querySelector('#compassZoomLevel').textContent, '128%');
  assert.ok(d.querySelector('#compassWrap').classList.contains('zoomed'));
  d.querySelector('#compassExpand').click();
  assert.ok(d.querySelector('#compassPanel').classList.contains('compass-expanded'));
  assert.strictEqual(d.querySelector('#compassExpand').getAttribute('aria-pressed'), 'true');
  d.dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  assert.ok(!d.querySelector('#compassPanel').classList.contains('compass-expanded'));
  d.querySelector('#compassReset').click();
  assert.strictEqual(d.querySelector('#compassZoomLevel').textContent, '100%');

  Object.defineProperty(d.querySelector('#treemap'), 'offsetWidth', {value: 800, configurable: true});
  Object.defineProperty(d.querySelector('#treemap'), 'offsetHeight', {value: 500, configurable: true});
  w.switchView('treemap');
  d.querySelector('#industrySearch').value = '行业甲';
  d.querySelector('#industrySearch').dispatchEvent(new w.Event('input', {bubbles: true}));
  assert.strictEqual(d.querySelector('#rowCount').textContent, '1 / 2 行业');
  assert.strictEqual(d.querySelector('#treemap').children.length, 1);
  assert.strictEqual(d.querySelector('#treemap').firstElementChild.dataset.industry, '行业甲');
  d.querySelector('#industrySearch').value = '';
  d.querySelector('#industrySearch').dispatchEvent(new w.Event('input', {bubbles: true}));
  w.switchView('table');

  assert.strictEqual(d.querySelector('[data-window="5"]').disabled, true);
  assert.strictEqual(d.querySelector('[data-window="20"]').disabled, true);
  w.windowDays = 20;
  w.renderAll();
  assert.ok(d.querySelector('#hero').textContent.includes('20日历史不足'));
  assert.strictEqual(w.marketMetric(w.currentMetrics()).cw, null);
  assert.strictEqual(w.marketMetric(w.currentMetrics()).priceEffect, null);
  assert.ok(d.querySelector('#tbody').textContent.includes('—'));
  assert.ok(d.querySelector('#tbody').textContent.includes('EW待确认'));

  w.windowDays = 1;
  w.activeDate = '2026-01-04';
  w.metricCache.clear();
  w.renderAll();
  w.openDrawer('行业甲');
  assert.ok(!d.querySelector('#stocksBody').textContent.includes('最新股票'));

  w.activeDate = '2026-01-05';
  w.metricCache.clear();
  w.renderAll();
  w.openDrawer('行业甲');
  const stockLink = d.querySelector('#stocksBody .stock-link');
  assert.strictEqual(stockLink.href, 'https://stockpage.10jqka.com.cn/000001/');
  assert.strictEqual(stockLink.rel, 'noopener noreferrer');
  assert.ok(stockLink.textContent.includes('最新股票'));
  assert.ok(stockLink.textContent.includes('000001'));
  assert.ok(!d.querySelector('#attributionBody').textContent.includes('+99.0 bp'));
  assert.ok(!d.querySelector('#attributionBody').textContent.includes('+77.0 bp'));
  assert.ok(d.querySelector('#attributionBody').textContent.includes('Share Snapshot 股本快照修订效应'));
  assert.ok(d.querySelector('#attributionBody').textContent.includes('行业级未提供'));
  assert.ok(!d.querySelector('#attributionBody').textContent.includes(' · high'));
  assert.ok(d.querySelector('#attributionBody').textContent.includes('绝不回填全市场数值'));
  assert.ok(d.querySelector('#qualityBody').textContent.includes('80.0%'));
  assert.ok(d.querySelector('#qualityBody').textContent.includes('2026-01-05'));
  assert.ok(d.querySelector('#attributionBody').textContent.includes('已包含在 Price，不重复相加'));
  w.showError('scheme failed');
  assert.strictEqual(d.querySelector('#dateStrip').children.length, 0);
  assert.strictEqual(d.querySelector('#structureBody').children.length, 0);
  assert.strictEqual(d.querySelector('#styleGrid').children.length, 0);
  assert.strictEqual(d.querySelector('#treemap').children.length, 0);
  assert.strictEqual(d.querySelector('#rowCount').textContent, '0 / 0 行业');
  assert.ok(d.querySelector('#hero').textContent.includes('scheme failed'));
  dom.window.close();

  function sizedPayload(dayCount) {
    const sized = clone(payload);
    const sizedDates = Array.from({length: dayCount}, (_, i) => {
      const day = String(i + 1).padStart(2, '0');
      return {date: `2026-02-${day}`, label: `2月${i + 1}日`};
    });
    const sizedMarket = sizedDates.map((date, i) => ({
      date: date.date,
      total_mcap: 100 + i,
      market_return_pct: 0.1,
      cap_weighted_return_pct: 0.1,
      equal_weight_return_pct: 0.1,
      stock_breadth_pct: 55,
      industry_breadth_pct: 100
    }));
    const sizedIndustry = sizedDates.map((date, i) => ({
      date: date.date,
      mcap: 50 + i,
      weight_pct: 50,
      cap_weighted_return_pct: 0.1,
      equal_weight_return_pct: 0.1,
      stock_breadth_pct: 55,
      contribution_bp: 1,
      stock_count: 10,
      stock_return_coverage_20d_pct: 100
    }));
    sized.trade_date = sizedDates[sizedDates.length - 1].date;
    sized.dates = sizedDates;
    sized.market.series = sizedMarket;
    sized.market.latest = sizedMarket[sizedMarket.length - 1];
    sized.industries[0].series = sizedIndustry;
    sized.industries[0].latest = sizedIndustry[sizedIndustry.length - 1];
    sized.data_quality.history_days = dayCount;
    return sized;
  }
  const chainLinked = sizedPayload(6);
  chainLinked.market.series.forEach((row, i, rows) => {
    const previous = i ? rows[i - 1].total_mcap : row.total_mcap;
    const dailyBp = i ? (row.total_mcap / previous - 1) * 10000 : 0;
    row.market_return_pct = dailyBp / 100;
    row.price_effect_bp = dailyBp;
    row.supply_effect_bp = 0;
    row.share_snapshot_effect_bp = 0;
    row.universe_effect_bp = 0;
    row.residual_effect_bp = 0;
    row.company_action_effect_bp = 0;
  });
  chainLinked.market.latest = chainLinked.market.series[chainLinked.market.series.length - 1];
  const chainDom = await boot(chainLinked);
  chainDom.window.windowDays = 5;
  chainDom.window.renderAll();
  assert.ok(Math.abs(chainDom.window.marketMetric(chainDom.window.currentMetrics()).priceEffect - 500) < 0.001);
  assert.strictEqual(chainDom.window.document.querySelector('#marketAttributionStatus').textContent, '恒等式核对通过');
  chainDom.window.close();

  const twentyDates = await boot(sizedPayload(20));
  assert.strictEqual(twentyDates.window.document.querySelector('[data-window="20"]').disabled, true);
  twentyDates.window.close();
  const twentyOneDates = await boot(sizedPayload(21));
  assert.strictEqual(twentyOneDates.window.document.querySelector('[data-window="20"]').disabled, false);
  twentyOneDates.window.close();

  const empty = await boot({
    schema_version: 2,
    scheme: 'sw',
    scheme_label: '申万一级',
    dates: [],
    market: {series: []},
    industries: [],
    data_quality: {}
  });
  assert.ok(empty.window.document.querySelector('#hero').textContent.includes('历史不足'));
  assert.strictEqual(empty.window.document.querySelector('#rowCount').textContent, '0 / 0 行业');
  assert.strictEqual(empty.window.document.querySelector('[data-window="1"]').disabled, true);
  empty.window.close();
  process.stdout.write('ok');
})().catch(error => {
  console.error(error.stack || error);
  process.exit(1);
});
"""
        result = subprocess.run(
            ["node", "-e", script],
            cwd=STATIC.parent,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok")

    def test_market_cap_has_exact_dates_windows_and_race_safe_schemes(self):
        source = (STATIC / "market-cap.html").read_text(encoding="utf-8")
        self.assertIn("pick(dateObj,['date','trade_date','full_label','label'],'')", source)
        self.assertIn("dateKey(machineDate(d))===dateKey(activeDate)", source)
        for value in ('value="sw"', 'value="ths"', 'value="sw3"'):
            self.assertIn(value, source)
        for window in ('data-window="1"', 'data-window="5"', 'data-window="20"'):
            self.assertIn(window, source)
        self.assertIn("new AbortController()", source)
        self.assertIn("generation!==requestGeneration", source)
        self.assertIn("next.scheme&&next.scheme!==scheme", source)
        self.assertIn("/api/market-cap?scheme=", source)

    def test_market_cap_discloses_measurement_boundaries(self):
        app = (STATIC / "app.html").read_text(encoding="utf-8")
        page = (STATIC / "market-cap.html").read_text(encoding="utf-8")
        for text in ("不是资金流入或净买入", "不伪造 Supply 供给效应",
                     "不等同于指数公司的自由流通口径"):
            self.assertIn(text, app)
        self.assertIn("不是资金流入或净买入", page)
        self.assertIn("circulating_share_proxy", page)
        self.assertIn("point_in_time_shares", page)
        self.assertIn("company_action_effect_bp", page)
        for text in ("A股流通市值代理", "历史行业归属按当前分类回溯",
                     "停牌价格承接", "时点股本覆盖"):
            self.assertIn(text, page)
            self.assertIn(text, app)
        self.assertIn("visibleQualityWarnings()", page)
        self.assertIn("suspension_carry||quality.suspensions||quality.suspended_prices", page)

    def test_intraday_width_controls_and_tooltip_are_present(self):
        source = (STATIC / "industry-heatmap.html").read_text(encoding="utf-8")
        self.assertIn("timelineSeriesVisibility.highs", source)
        self.assertIn("function toggleTimelineSeries(series, visible)", source)
        self.assertIn("timeline-toggle", source)
        self.assertIn("每5分钟新增触及", source)
        self.assertIn("同一刻度", source)
        self.assertIn("retained", source)
        self.assertIn("严格站稳高 / 低", source)
        self.assertIn("盘中观察 · 尚待量价确认", source)
        self.assertIn("严格' + row.high_count", source)
        self.assertNotIn("xlsx.full.min.js", source)

    def test_heatmap_is_evidence_gated_opportunity_view(self):
        source = (STATIC / "industry-heatmap.html").read_text(encoding="utf-8")
        for text in (
            "行业机会热力图",
            'data-direction="opportunity"',
            "/api/heatmap-opportunities",
            "状态迁移候选",
            "数据闸门",
            "失效条件",
            "样本不足",
            "不显示伪成功率",
        ):
            self.assertIn(text, source)
        self.assertIn("heatmap_opportunity.py", Path("electron-builder.yml").read_text(encoding="utf-8"))

    def test_opportunity_view_keeps_the_original_heatmap_below(self):
        source = (STATIC / "industry-heatmap.html").read_text(encoding="utf-8")
        for text in (
            "原始热力矩阵 · 与机会状态同步",
            "创新高矩阵",
            "创新低矩阵",
            "opportunityRawMatrix",
            "opportunityRawHeatmapTable",
            "switchOpportunityRaw",
        ):
            self.assertIn(text, source)

    def test_opportunity_industry_name_opens_latest_high_detail(self):
        source = (STATIC / "industry-heatmap.html").read_text(encoding="utf-8")
        for text in (
            'class="industry-high-link"',
            "function openLatestIndustryHighs(industry)",
            "latest.label || latest.full_label",
            "currentDirection = 'highs'",
            "tableData = highsData",
            "openDetail(industry, dateLabel, count)",
            "industryHighLink.getAttribute('data-industry')",
        ):
            self.assertIn(text, source)
        self.assertLess(
            source.index("var industryHighLink = e.target.closest('.industry-high-link')"),
            source.index("var opportunityRow = e.target.closest('.opportunity-row')"),
        )
        guide = (STATIC / "page-guide.js").read_text(encoding="utf-8")
        self.assertIn("点击行业名可直接打开该行业最新一期创新高个股列表", guide)

    def test_heatmap_quadrant_uses_gated_relative_metrics(self):
        source = (STATIC / "industry-heatmap.html").read_text(encoding="utf-8")
        for text in (
            "规模收缩后净扩散（横轴）",
            "成交参与历史分位（纵轴）",
            "adjusted_net_breadth_pct",
            "activity_percentile",
            "turnover_amount",
            "quality.flow_aligned",
            "var labelLimit",
            "quadrant-crowded",
            "偏在一侧是当日真实扩散结果",
        ):
            self.assertIn(text, source)
        self.assertNotIn("同日成交活跃变化（纵轴）", source)
        self.assertNotIn("var maxAbsNet = 20;", source)

    def test_every_business_page_has_a_collapsible_usage_guide(self):
        pages = (
            "opportunity-summary.html",
            "industry-heatmap.html",
            "market-temperature.html",
            "index-constituents.html",
            "crowding.html",
            "capital-flow.html",
            "margin-financing.html",
            "market-cap.html",
            "etf-recommend.html",
            "momentum-etf.html",
            "etf-backtest.html",
        )
        for filename in pages:
            source = (STATIC / filename).read_text(encoding="utf-8")
            self.assertIn('<script src="/page-guide.js" defer></script>', source, filename)
        guide = (STATIC / "page-guide.js").read_text(encoding="utf-8")
        for text in (
            "page-guide-details",
            "pageUsageGuide",
            "机会汇总使用说明",
            "行业机会热力图使用说明",
            "市场参与强度使用说明",
            "股指成分监控使用说明",
            "交易拥挤度使用说明",
            "成交动能使用说明",
            "板块融资融券使用说明",
            "市值结构与行情归因使用说明",
            "ETF 下一热点使用说明",
            "动量 ETF 使用说明",
            "ETF 回测使用说明",
        ):
            self.assertIn(text, guide)
        self.assertIn("document.querySelector('.container')", guide)

    def test_every_business_page_has_a_data_status_bar(self):
        pages = (
            "opportunity-summary.html",
            "industry-heatmap.html",
            "market-temperature.html",
            "index-constituents.html",
            "crowding.html",
            "capital-flow.html",
            "margin-financing.html",
            "market-cap.html",
            "etf-recommend.html",
            "momentum-etf.html",
            "etf-backtest.html",
        )
        for filename in pages:
            source = (STATIC / filename).read_text(encoding="utf-8")
            self.assertIn('<script src="/data-status.js" defer></script>', source, filename)
        standalone = (STATIC / "industry-heatmap-standalone.html").read_text(encoding="utf-8")
        self.assertNotIn("/data-status.js", standalone)
        script = (STATIC / "data-status.js").read_text(encoding="utf-8")
        for text in (
            "/api/refresh-data/check",
            "/api/refresh-data",
            "/api/refresh-data/status",
            "dataStatusBar",
            "window !== window.top",
            "location.reload()",
        ):
            self.assertIn(text, script)

    def test_content_pages_distinguish_reload_from_update(self):
        pages = (
            "crowding.html",
            "capital-flow.html",
            "margin-financing.html",
            "market-cap.html",
            "opportunity-summary.html",
            "market-temperature.html",
        )
        for filename in pages:
            source = (STATIC / filename).read_text(encoding="utf-8")
            self.assertIn("↻ 重新加载", source, filename)
            self.assertNotIn("刷新数据", source, filename)

    def test_theme_system_is_wired_into_every_page(self):
        pages = (
            "app.html",
            "opportunity-summary.html",
            "industry-heatmap.html",
            "market-temperature.html",
            "index-constituents.html",
            "crowding.html",
            "capital-flow.html",
            "margin-financing.html",
            "market-cap.html",
            "etf-recommend.html",
            "momentum-etf.html",
            "etf-backtest.html",
        )
        for filename in pages:
            source = (STATIC / filename).read_text(encoding="utf-8")
            self.assertIn('<script src="/theme.js"></script>', source, filename)
        standalone = (STATIC / "industry-heatmap-standalone.html").read_text(encoding="utf-8")
        self.assertNotIn("/theme.js", standalone)
        themes = (STATIC / "themes.css").read_text(encoding="utf-8")
        self.assertIn('html[data-theme="light"]', themes)
        # ":root" 是 html 本身,"html[data-theme] :root" 后代选择器永不命中,
        # 变量必须直接定义在 html[data-theme] 规则里
        self.assertNotIn("] :root", themes)
        self.assertIn("--bg: #f4f5f7", themes)
        for variable in ("--bg", "--panel", "--text", "--muted", "--border", "--accent"):
            self.assertIn(variable, themes)
        script = (STATIC / "theme.js").read_text(encoding="utf-8")
        for text in ("localStorage", "storage", "AppTheme", "data-theme", "/themes.css"):
            self.assertIn(text, script)
        app = (STATIC / "app.html").read_text(encoding="utf-8")
        for text in ('id="sec-theme"', "theme-card", "setTheme", 'data-theme="light"'):
            self.assertIn(text, app)
        for component in ("data-status.js", "page-guide.js"):
            source = (STATIC / component).read_text(encoding="utf-8")
            self.assertIn("var(--panel", source, component)

    def test_opportunity_summary_is_integrated_into_the_desktop_shell(self):
        app = (STATIC / "app.html").read_text(encoding="utf-8")
        server = Path("server.py").read_text(encoding="utf-8")
        builder = Path("electron-builder.yml").read_text(encoding="utf-8")
        for text in (
            'data-tab="summary"',
            'id="panel-summary"',
            "/opportunity-summary.html",
            "DATA_FRAME_PATHS",
            "function reloadSummaryFrame",
            "iframe.contentWindow.location",
            "new URLSearchParams(current.search)",
            "['scheme', 'period', 'mode']",
            "window.electronAPI.onDataUpdated",
        ):
            self.assertIn(text, app)
        self.assertIn('@app.get("/api/opportunity-summary")', server)
        self.assertIn("build_opportunity_summary", server)
        self.assertIn("opportunity_summary.py", builder)

    def test_opportunity_summary_preserves_evidence_and_safe_drilldowns(self):
        page = (STATIC / "opportunity-summary.html").read_text(encoding="utf-8")
        for text in (
            "/api/opportunity-summary?",
            "independent_confirmations",
            "confirmation_total",
            "laneLimits:{confirmed:10,watch:10,rejected:10}",
            "market.message || market.reason",
            "function syncSourceFrame",
            "view.switchScheme(context.scheme)",
            '#modeToggle [data-mode="',
            '#typeToggle [data-type="',
            "非触发域",
            "同一证据域内的同源衍生指标只计一票",
            "https://stockpage.10jqka.com.cn/",
            'target="_blank"',
            'rel="noopener noreferrer"',
            r"/(^|\D)(\d{6})(?!\d)/",
            "无合格 ETF · 仅参考",
            "原页为申万三级 · 当前不跳转",
            '<script src="/page-guide.js" defer></script>',
        ):
            self.assertIn(text, page)
        self.assertIn("return state.candidates.filter(function(row)", page)
        filtered = page.split("function filteredCandidates()", 1)[1].split(
            "function renderCandidates()", 1
        )[0]
        self.assertNotIn(".sort(", filtered)

    def test_standalone_heatmap_embeds_all_schemes_and_opportunity_states(self):
        source = Path("generate_standalone.py").read_text(encoding="utf-8")
        for text in (
            '"sw": ""',
            '"ths": "_ths"',
            '"sw3": "_sw3"',
            "load_opportunity_snapshot",
            "STANDALONE_OPPORTUNITIES",
            "独立收盘快照",
            "page-guide.js",
            "guide_script",
        ):
            self.assertIn(text, source)
        self.assertNotIn("xlsx.full.min.js", source)

    def test_detail_modal_core_stock_columns_are_sortable(self):
        source = (STATIC / "industry-heatmap.html").read_text(encoding="utf-8")
        for key in ("code", "name", "price", "change_pct"):
            self.assertIn("sortableStockHeader", source)
            self.assertIn("'" + key + "'", source)
        self.assertIn("localeCompare(textB, 'zh-CN'", source)

    def test_auto_refresh_pauses_outside_trading_session(self):
        source = (STATIC / "industry-heatmap.html").read_text(encoding="utf-8")
        self.assertIn("async function scheduledRefreshTick()", source)
        self.assertIn("state.phase === 'trading'", source)
        self.assertIn("自动已暂停 · 非交易时段", source)
        self.assertIn("setInterval(scheduledRefreshTick, 60000)", source)
        self.assertNotIn("if (currentMode === 'auto' || effectiveMode === 'intraday') loadData();", source)

    def test_settings_actions_have_visible_status_targets(self):
        source = (STATIC / "app.html").read_text(encoding="utf-8")
        self.assertNotIn("apiStatus", source)
        self.assertIn('id="aiStatus"', source)
        self.assertIn('id="mcpStatus"', source)
        self.assertIn("'etf','ai','standalone'", source)

    def test_settings_status_and_sensitive_inputs_are_unambiguous(self):
        source = (STATIC / "app.html").read_text(encoding="utf-8")
        self.assertIn('type="password" id="apiKeyInput"', source)
        self.assertIn("shortTradeDate(actual) + ' → ' + shortTradeDate(expected)", source)
        self.assertIn("fetchJsonChecked('/api/backup'", source)
        self.assertIn("await saveBackupDir()", source)
        self.assertIn("data.analysis_source === 'llm'", source)
        self.assertIn('role="tab" aria-selected="true"', source)
        self.assertNotIn("height: calc(100% - 41px)", source)

    def test_turnover_momentum_exposes_first_and_second_stage_views(self):
        app = (STATIC / "app.html").read_text(encoding="utf-8")
        page = (STATIC / "capital-flow.html").read_text(encoding="utf-8")
        self.assertIn("成交动能", app)
        self.assertNotIn("💰 资金流向", app)
        for text in ("量价罗盘", "相对参与历史分位", "方向压力", "有效参与",
                     "持续性", "加速度", "量价效率", "当前风险证据",
                     "20 日轨迹", "成交贡献 Top5 股票"):
            self.assertIn(text, page)
        self.assertIn('id="quadrantFilter"', page)
        self.assertIn('id="stateFilter"', page)
        self.assertIn('class="drawer"', page)
        self.assertIn("riskReasons", page)
        self.assertIn("efficiency_gap", page)
        self.assertIn("coverageRaw&&typeof coverageRaw==='object'", page)
        self.assertIn("var persistence=finite(", page)
        self.assertNotIn("var persistence=score100(", page)
        self.assertEqual(page.count("function renderHero("), 1)

    def test_turnover_momentum_compass_can_expand_box_zoom_pan_and_reset(self):
        page = (STATIC / "capital-flow.html").read_text(encoding="utf-8")
        for text in (
            'id="compassPanel"',
            'id="compassZoomOut"',
            'id="compassZoomIn"',
            'id="compassReset"',
            'id="compassBoxZoom"',
            'id="compassExpand"',
            '.compass-panel.compass-expanded',
            'var compassView = {scale:1, centerX:0, centerY:50}',
            'function zoomCompassAt(multiplier,anchorX,anchorY)',
            'function closestCompassBubble(clientX,clientY)',
            'function resetCompassView(shouldRender)',
            'function toggleCompassExpanded(force)',
            "wrap.addEventListener('wheel'",
            "wrap.addEventListener('pointerdown'",
            "wrap.addEventListener('pointermove'",
            'class="bubble-hit"',
            "currentScheme==='sw3'?14:12",
            'class="compass-selection"',
            'resetCompassView(false)',
            '支持滚轮缩放、框选放大和拖拽平移',
        ):
            self.assertIn(text, page)
        guide = (STATIC / "page-guide.js").read_text(encoding="utf-8")
        self.assertIn("罗盘放大与点选", guide)
        self.assertIn("点“框选”后在图中拖出矩形", guide)

    def test_turnover_momentum_scheme_switch_is_race_safe_and_timestamped(self):
        page = (STATIC / "capital-flow.html").read_text(encoding="utf-8")
        self.assertIn("requestGeneration", page)
        self.assertIn("new AbortController()", page)
        self.assertIn("generation!==requestGeneration", page)
        self.assertIn("next.scheme && next.scheme!==scheme", page)
        self.assertIn("/api/capital-flow?scheme=", page)
        self.assertIn("['updated_at','generated_at','as_of']", page)
        for value in ('value="sw"', 'value="ths"', 'value="sw3"'):
            self.assertIn(value, page)

    def test_turnover_momentum_matches_v2_series_by_canonical_date(self):
        page = (STATIC / "capital-flow.html").read_text(encoding="utf-8")
        self.assertIn(
            "pick(dateObj,['date','trade_date','full_label','label'],'—')",
            page,
        )
        self.assertIn(
            "pick(selected,['full_label','date','trade_date','label'],'—')",
            page,
        )

    def test_refresh_completion_reloads_turnover_momentum(self):
        app = (STATIC / "app.html").read_text(encoding="utf-8")
        self.assertIn("if (needFlow || steps.length === 0)", app)
        self.assertIn("document.querySelector('#panel-flow iframe').src = '/capital-flow.html?_=' + Date.now()", app)

    def test_three_industry_schemes_are_available_on_all_views(self):
        for filename in ("industry-heatmap.html", "capital-flow.html", "margin-financing.html", "market-cap.html"):
            source = (STATIC / filename).read_text(encoding="utf-8")
            self.assertIn('option value="sw3"', source)
        heatmap = (STATIC / "industry-heatmap.html").read_text(encoding="utf-8")
        self.assertIn("schemeSuffix(currentScheme)", heatmap)
        self.assertIn("/api/custom-heatmap?window=' + currentWindow + '&scheme=' + currentScheme", heatmap)

    def test_every_individual_stock_detail_view_links_to_ths(self):
        pages = {
            filename: (STATIC / filename).read_text(encoding="utf-8")
            for filename in (
                "index.html",
                "industry-heatmap.html",
                "index-constituents.html",
                "crowding.html",
                "capital-flow.html",
                "margin-financing.html",
                "market-cap.html",
            )
        }
        for source in pages.values():
            self.assertIn("function stockLink(", source)
            self.assertIn("https://stockpage.10jqka.com.cn/", source)
            self.assertIn('target="_blank"', source)
            self.assertIn('rel="noopener noreferrer"', source)
            self.assertIn(r"/^\d{6}$/", source)
        self.assertIn("stockLink(item.code,escapeHtml(label),label)", pages["industry-heatmap.html"])
        self.assertIn("stockLink(q.code, stockTitle, name)", pages["index.html"])
        self.assertIn("stockMetricHtml('首要驱动',topPositive)", pages["index-constituents.html"])
        self.assertIn("return stockLink(code, escapeHtml(label), label)", pages["crowding.html"])
        self.assertIn("var stockCell=stockLink(", pages["capital-flow.html"])
        self.assertIn("stockLink(r.code,r.name,r.name)", pages["margin-financing.html"])
        self.assertIn("var stockCell=stockLink(", pages["market-cap.html"])

    def test_margin_financing_page_is_integrated_and_drills_down_to_ths(self):
        page = (STATIC / "margin-financing.html").read_text(encoding="utf-8")
        app = (STATIC / "app.html").read_text(encoding="utf-8")
        server = Path("server.py").read_text(encoding="utf-8")
        builder = Path("electron-builder.yml").read_text(encoding="utf-8")
        for text in (
            'option value="sw"',
            'option value="ths"',
            'option value="sw3"',
            "/api/margin-financing?scheme=",
            "industry-link",
            "https://stockpage.10jqka.com.cn/",
            "融资余额",
            "融资买入额",
            "买入强度",
            "融券余额",
        ):
            self.assertIn(text, page)
        for text in (
            'data-tab="margin"',
            'id="panel-margin"',
            "/margin-financing.html",
            'value="margin_financing"',
            "function runMarginFinancing()",
        ):
            self.assertIn(text, app)
        self.assertIn('@app.get("/api/margin-financing")', server)
        self.assertIn('from: "margin_financing.py"', builder)

    def test_index_constituents_is_an_evidence_gated_futures_monitor(self):
        source = (STATIC / "index-constituents.html").read_text(encoding="utf-8")
        for text in (
            "IH · 上证50",
            "IF · 沪深300",
            "IC · 中证500",
            "IM · 中证1000",
            "当前证据状态",
            "行业贡献",
            "方向与速度",
            "内部广度",
            "贡献结构",
            "期现确认",
            "贡献表格",
            "市值树图",
            "指数权重",
            "绝对贡献",
            "流通市值",
            "同花顺细分",
            "申万一级",
            "申万三级",
            "原始基差不能直接解释未来涨跌",
        ):
            self.assertIn(text, source)
        self.assertIn("/api/index-futures-overview", source)
        self.assertIn("/api/index-constituents?index=", source)
        self.assertIn("state.data?.quality", source)
        self.assertIn("replication_residual_bp", source)
        self.assertIn("effective_live_weight_pct", source)
        self.assertIn("contribution_change_${window}_bp", source)
        self.assertIn("quote_state", source)
        self.assertIn("Promise.all([loadOverview(false),loadData(true,true)])", source)
        self.assertIn("https://stockpage.10jqka.com.cn/", source)

    def test_etf_recommendation_is_selective_etf_level_and_v3_compatible(self):
        source = (STATIC / "etf-recommend.html").read_text(encoding="utf-8")
        self.assertIn("etf_recommend_sw3.json", source)
        self.assertIn("ETF热点候选与确认", source)
        self.assertIn("允许无信号", source)
        self.assertIn("今日没有通过门槛的 ETF 热点", source)
        self.assertIn("未来5日命中概率", source)
        self.assertIn("概率不足时不显示伪百分比", source)
        self.assertIn("p === null", source)
        self.assertIn("text:'校准中'", source)
        self.assertIn("opportunity_score", source)
        self.assertIn("decision_status", source)
        self.assertIn("actionable_message", source)
        self.assertIn("raw.etfs", source)
        self.assertIn("raw.top", source)
        for stage in ("confirmed", "emerging", "watch", "avoid", "insufficient"):
            self.assertIn(stage, source)
        for signal in ("regime", "demand", "diffusion", "relative_strength", "tradability"):
            self.assertIn(signal, source)
        self.assertIn("ETF份额变化", source)
        self.assertIn("share_change_pct", source)
        self.assertIn("relative_5d", source)
        self.assertIn("数据可信度", source)
        self.assertIn("载体匹配度", source)
        self.assertIn("primary_level", source)
        self.assertIn("代理映射", source)
        self.assertIn("失效条件", source)
        self.assertIn("related_industries", source)
        self.assertIn("mergeUniqueRows", source)
        self.assertIn("旧模型 · 仅观察", source)
        self.assertIn("二级回退", source)
        self.assertIn("一级回退", source)
        self.assertIn("https://stockpage.10jqka.com.cn/", source)
        self.assertIn("function escapeHtml(value)", source)
        self.assertIn("escapeHtml(error && error.message", source)
        self.assertNotIn("推荐 Top10", source)

    def test_momentum_matches_reference_rank_engine_and_trajectory(self):
        source = (STATIC / "momentum-etf.html").read_text(encoding="utf-8")
        self.assertIn("https://stockpage.10jqka.com.cn/", source)
        for text in (
            'id="chart"',
            "function renderChart()",
            "function computeDays()",
            "短期年化下限 (%)",
            "启用盈利保护",
            "盈利保护回撤 (%)",
            "倒序排名（分数从低到高）",
            'const DEFAULT_GROUP = "wufu"',
            "const rows = Math.min(cfg.topN",
            "members.get(m.code).points.push",
            '"首日"',
            '"最新"',
            'esc(clip(info.name, 8)) + " 退出"',
            "prevRank === undefined || prevRank > state.cfg.topN",
            "segment.map((p) => x(p.i) + \",\" + y(p.rank))",
            "Math.max(cfg.lookbackDays, cfg.shortLookbackDays, cfg.profitProtectionLookback)",
            "refresh(false)",
        ):
            self.assertIn(text, source)
        self.assertNotIn("排名轨迹已开始累计", source)

    def test_momentum_uses_the_shared_dashboard_visual_language(self):
        source = (STATIC / "momentum-etf.html").read_text(encoding="utf-8")
        for text in (
            'class="shell"',
            'class="topbar"',
            'class="toolbar"',
            'class="status-panel"',
            'id="overviewCards" class="metric-grid"',
            'class="panel-head"',
            'class="panel-title"',
            'class="table-scroll"',
            "max-width:1580px",
            "--bg:#0c111a",
            "--panel:#131b28",
            "--accent:#6ea8fe",
            "--up:#ef6a67",
            "--down:#52a7d8",
            "function renderOverview()",
            'cssColor("--muted"',
            '红 = 上涨　蓝 = 下跌',
        ):
            self.assertIn(text, source)
        self.assertNotIn("max-width: 960px", source)
        self.assertNotIn("position: sticky; top: 0", source)

    def test_settings_guide_covers_current_modules_and_metric_boundaries(self):
        source = (STATIC / "app.html").read_text(encoding="utf-8")
        for text in ("市场参与强度", "股指成分", "成交集中与拥挤风险", "方向压力",
                     "机会分", "动态池", "T+1开盘", "申万三级", "市值树图", "同花顺"):
            self.assertIn(text, source)
        self.assertIn("不是逐笔主动买卖、主力净流入或真实资金流向", source)
        self.assertIn("任何单一分数都不应直接当作买卖指令", source)
        self.assertIn('class="guide-details" open', source)
        self.assertIn("@media (max-width: 720px)", source)

    def test_market_strength_uses_stable_causal_language_and_session_gate(self):
        source = (STATIC / "market-temperature.html").read_text(encoding="utf-8")
        self.assertIn("市场参与强度", source)
        self.assertIn("CORE_WEIGHTS = {breadth: 0.60, limit: 0.40}", source)
        self.assertIn("DATA.session === 'trading'", source)
        self.assertIn("sameDay(intraday.trade_date, nowKey)", source)
        self.assertIn("辅助因子最短", source)
        self.assertIn("同一百分比坐标下并列展示", source)
        self.assertNotIn("指数涨跌堆叠", source)

    def test_crowding_separates_concentration_from_exit_danger(self):
        source = (STATIC / "crowding.html").read_text(encoding="utf-8")
        self.assertIn("成交集中与拥挤风险", source)
        self.assertIn("不代表持仓拥挤、资金净流入或必然反转", source)
        self.assertIn("红色仅表示增强数据明确确认“去拥挤中”", source)
        self.assertIn("旧版 JSON 只有成交额集中字段时", source)
        self.assertIn("function normalizeRisk", source)
        self.assertIn("融资余额 / 日成交", source)
        self.assertIn("外部证据未提供；当前风险判断仅使用成交、价格与宽度代理", source)
        self.assertIn("北向逐股日频披露已停止，不参与评分", source)
        self.assertIn("function hhiContributionValue", source)
        self.assertIn("concentrationDomains.reduce", source)
        self.assertIn("证据不足|unknown|insufficient", source)
        for value in ('value="sw"', 'value="ths"', 'value="sw3"'):
            self.assertIn(value, source)
        self.assertIn("async function switchScheme", source)
        self.assertIn("/api/crowding?scheme=", source)
        self.assertIn("三套口径分别计算历史分位与 HHI", source)

    def test_crowding_risk_matrix_can_expand_zoom_pan_and_reset(self):
        source = (STATIC / "crowding.html").read_text(encoding="utf-8")
        for text in (
            'id="matrixZoomOut"',
            'id="matrixZoomIn"',
            'id="matrixReset"',
            'id="matrixExpand"',
            'class="matrix-zoom-level"',
            '.matrix-panel.matrix-expanded',
            'const MATRIX_VIEW = {minX:0, maxX:100, minY:0, maxY:100}',
            'function zoomMatrixAt(factor, centerX, centerY)',
            "svg.addEventListener('wheel'",
            "svg.addEventListener('pointerdown'",
            "svg.addEventListener('pointermove'",
            'function resetMatrixView(shouldRender=true)',
            "event.key === 'Escape'",
            "'data-matrix-industry':industry.name",
            'r:Math.max(radius + 5, 12)',
            "fill:'transparent', stroke:'none', 'pointer-events':'all'",
            'resetMatrixView(false)',
        ):
            self.assertIn(text, source)
        self.assertIn('aria-pressed="false"', source)
        self.assertIn('支持滚轮缩放和拖拽平移', source)
        guide = (STATIC / "page-guide.js").read_text(encoding="utf-8")
        self.assertIn("矩阵放大操作", guide)
        self.assertIn("气泡的可点击范围略大于可见圆点", guide)

    def test_etf_backtest_uses_strict_out_of_sample_metrics_and_disables_intraday_proxy(self):
        source = (STATIC / "etf-backtest.html").read_text(encoding="utf-8")
        self.assertIn('id="tabIntraday"', source)
        self.assertIn("module_ === 'intraday'", source)
        self.assertIn("14:50 策略回测已停用", source)
        self.assertIn("日线数据无法无穿越复现", source)
        self.assertIn("round_trip_bps", source)
        self.assertIn("DATA.benchmark", source)
        self.assertIn("excess_ret_t5", source)
        self.assertIn("precision_at_k", source)
        self.assertIn("rank_ic", source)
        self.assertIn("按预测日聚类", source)
        self.assertIn("校准中", source)
        self.assertIn("SHORT_HORIZONS = [1, 2, 3, 4, 5]", source)
        for day in range(1, 6):
            self.assertIn(f"T+{day} 净", source)
        self.assertIn("概率统一使用 T+5 标签", source)
        self.assertNotIn("T+10", source)
        self.assertNotIn("t10", source)
        self.assertNotIn("累计净值曲线", source)
        self.assertNotIn("+1% 止盈", source)


if __name__ == "__main__":
    unittest.main()
