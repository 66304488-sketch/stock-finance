/* 主题应用器:首帧前设置 data-theme,localStorage 持久化,
 * storage 事件让同源的已开页面与仪表盘 iframe 即时联动换肤。
 * 需以非 defer 方式放在 <head> 最前面。离线页 themes.css 加载失败时保持深色。 */
(function () {
  'use strict';

  var KEY = 'app-theme';
  var THEMES = ['dark', 'light'];

  function read() {
    try {
      var value = localStorage.getItem(KEY);
      return THEMES.indexOf(value) >= 0 ? value : 'dark';
    } catch (e) {
      return 'dark';
    }
  }

  function apply(name) {
    document.documentElement.dataset.theme = name;
  }

  function set(name) {
    if (THEMES.indexOf(name) < 0) name = 'dark';
    try {
      localStorage.setItem(KEY, name);
    } catch (e) { /* 隐私模式等场景:仅当前页生效 */ }
    apply(name);
  }

  apply(read());

  var link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/themes.css';
  link.onerror = function () { link.remove(); };
  document.head.appendChild(link);

  window.addEventListener('storage', function (event) {
    if (event.key !== KEY) return;
    var previous = document.documentElement.dataset.theme;
    apply(read());
    // 图表 canvas 与部分文字颜色在 JS 里按主题计算,切换后重载保证一致
    if (document.documentElement.dataset.theme !== previous) {
      location.reload();
    }
  });

  window.AppTheme = {
    get: read,
    set: set,
    themes: THEMES.slice()
  };
})();
