/* 全局数据状态条:显示数据截至日期与落后天数,一键触发真正的数据更新。
 *
 * 与页面上的"重新加载"区分:重新加载只重读已生成的数据;
 * 这里的"更新数据"会调用 /api/refresh-data 拉取最新行情并重建数据。
 * 离线页(standalone)或服务未启动时静默不渲染。
 */
(function () {
  'use strict';

  var CORE_DATASETS = ['highs', 'lows', 'capital_flow', 'margin_financing', 'market_cap'];
  var monitoredDatasets = CORE_DATASETS.slice();
  var POLL_INTERVAL_MS = 1200;
  var POLL_TIMEOUT_MS = 20 * 60 * 1000;
  var poll = { running: false, failures: 0, startedAt: 0, missingDays: 1 };

  function shortDate(value) {
    value = String(value || '');
    return value.length === 8 ? value.slice(4, 6) + '/' + value.slice(6, 8) : '';
  }

  function addStyle() {
    if (document.getElementById('dataStatusStyles')) return;
    var style = document.createElement('style');
    style.id = 'dataStatusStyles';
    style.textContent = [
      '.ds-bar{position:fixed;top:8px;right:8px;z-index:900;display:flex;align-items:center;gap:8px;',
      'padding:5px 10px;border:1px solid var(--border,#343a46);border-radius:999px;background:var(--panel,rgba(31,35,41,.92));',
      'color:var(--text,#abb2bf);font:12px/1.4 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;',
      'box-shadow:0 2px 10px rgba(0,0,0,.25)}',
      '.ds-dot{width:8px;height:8px;border-radius:50%;background:#7f8792;flex:none}',
      '.ds-dot.ok{background:#52c47a}.ds-dot.stale{background:#e5c07b}.ds-dot.err{background:#e06c75}',
      '.ds-btn{border:1px solid var(--border,#3f4654);border-radius:999px;background:var(--bg-tab,#282c34);color:var(--accent,#61afef);',
      'font-size:12px;padding:2px 10px;cursor:pointer}',
      '.ds-btn:hover:not(:disabled){background:var(--panel-2,#31363f)}',
      '.ds-btn:disabled{opacity:.55;cursor:default}',
      '@media(max-width:640px){.ds-text-full{display:none}}'
    ].join('');
    document.head.appendChild(style);
  }

  function els() {
    return {
      dot: document.getElementById('dsDot'),
      text: document.getElementById('dsText'),
      btn: document.getElementById('dsBtn')
    };
  }

  function setStatus(dotClass, text, title) {
    var ui = els();
    ui.dot.className = 'ds-dot ' + dotClass;
    ui.text.textContent = text;
    ui.text.title = title || '';  // 无 title 时清除旧提示
  }

  function renderCheck(data) {
    var states = (data && data.datasets) || {};
    var expected = (data && data.latest_trade_date) || '';
    var lastDates = [];
    var missing = 0;
    monitoredDatasets.forEach(function (key) {
      var st = states[key];
      if (st && /^\d{8}$/.test(st.last_date || '')) lastDates.push(st.last_date);
      if (st && st.status === 'needs_update') missing = Math.max(missing, st.missing_count || 0);
    });
    var actual = lastDates.length ? lastDates.sort()[0] : '';
    var stale = monitoredDatasets.some(function (key) {
      return !states[key] || states[key].status !== 'up_to_date';
    });
    poll.missingDays = Math.min(Math.max(missing, 1), 30);
    if (stale) {
      setStatus(
        'stale',
        actual
          ? '数据 ' + shortDate(actual) + ' → ' + shortDate(expected) + ' · 落后' + poll.missingDays + '天'
          : '数据待更新',
        '受监控数据最早只到 ' + (actual || '无') + '，应更新至 ' + expected
      );
    } else {
      setStatus('ok', '数据 ' + shortDate(expected), '受监控数据已更新至 ' + expected);
    }
  }

  async function loadCheck() {
    try {
      var responses = await Promise.all([
        fetch('/api/refresh-data/check?_=' + Date.now()),
        fetch('/api/update-config?_=' + Date.now()).catch(function () { return null; })
      ]);
      if (!responses[0].ok) throw new Error('HTTP ' + responses[0].status);
      if (responses[1] && responses[1].ok) {
        var strategy = await responses[1].json();
        var selected = strategy && strategy.config && strategy.config.selected_datasets;
        if (Array.isArray(selected)) {
          var configuredCore = selected.filter(function (key) { return CORE_DATASETS.indexOf(key) >= 0; });
          if (configuredCore.length) monitoredDatasets = configuredCore;
        }
      }
      renderCheck(await responses[0].json());
      document.getElementById('dataStatusBar').style.display = 'flex';
    } catch (e) {
      /* 离线页或服务未启动:保持隐藏 */
    }
  }

  function finishButton(label) {
    var ui = els();
    ui.btn.disabled = false;
    ui.btn.textContent = label || '🔄 更新数据';
  }

  async function pollStatus() {
    try {
      var resp = await fetch('/api/refresh-data/status?_=' + Date.now());
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      var data = await resp.json();
      poll.failures = 0;
      if (data.running) {
        if (Date.now() - poll.startedAt > POLL_TIMEOUT_MS) throw new Error('更新超时');
        setStatus('stale', data.current_step || '更新中…');
        setTimeout(pollStatus, POLL_INTERVAL_MS);
        return;
      }
      poll.running = false;
      if (data.success) {
        setStatus('ok', '更新完成,重新加载…');
        setTimeout(function () { location.reload(); }, 600);
      } else {
        setStatus('err', '更新失败', data.error || '');
        finishButton();
      }
    } catch (e) {
      poll.failures += 1;
      if (poll.failures < 5 && Date.now() - poll.startedAt <= POLL_TIMEOUT_MS) {
        setTimeout(pollStatus, 2000);
        return;
      }
      poll.running = false;
      setStatus('err', '无法确认更新状态', e.message || '');
      finishButton();
    }
  }

  async function runUpdate() {
    if (poll.running) return;
    var ui = els();
    ui.btn.disabled = true;
    ui.btn.textContent = '更新中…';
    try {
      var resp = await fetch('/api/refresh-data', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          days: poll.missingDays
        })
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      var result = await resp.json();
      if (result.status !== 'started' && result.status !== 'already_running') {
        throw new Error(result.error || '启动失败');
      }
      poll.running = true;
      poll.failures = 0;
      poll.startedAt = Date.now();
      setStatus('stale', '更新中…');
      setTimeout(pollStatus, POLL_INTERVAL_MS);
    } catch (e) {
      setStatus('err', '更新启动失败', e.message || '');
      finishButton();
    }
  }

  function render() {
    if (document.getElementById('dataStatusBar')) return;
    if (window !== window.top) return; // 被嵌入 app.html 仪表盘时不重复渲染
    addStyle();
    var bar = document.createElement('div');
    bar.className = 'ds-bar';
    bar.id = 'dataStatusBar';
    bar.style.display = 'none';
    bar.innerHTML =
      '<span class="ds-dot" id="dsDot"></span>' +
      '<span class="ds-text-full" id="dsText">数据状态…</span>' +
      '<button type="button" class="ds-btn" id="dsBtn" ' +
      'title="拉取最新行情并重建数据(真正更新);页面上的「重新加载」只重读已有数据">🔄 更新数据</button>';
    document.body.appendChild(bar);
    document.getElementById('dsBtn').addEventListener('click', runUpdate);
    loadCheck();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', render);
  } else {
    render();
  }
})();
