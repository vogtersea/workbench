/* 深色/浅色主题切换：跟随系统，可手动覆盖并记住选择。
   首屏引导脚本在 base.html 的 <head> 内联执行（避免白屏闪烁），这里只处理交互。 */
(function () {
  "use strict";

  var KEY = "wb-theme";
  var root = document.documentElement;

  function systemPrefersDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function current() {
    var explicit = root.getAttribute("data-theme");
    if (explicit === "dark" || explicit === "light") return explicit;
    return systemPrefersDark() ? "dark" : "light";
  }

  function apply(theme) {
    if (theme === "system") {
      root.removeAttribute("data-theme");
      try { localStorage.removeItem(KEY); } catch (e) {}
    } else {
      root.setAttribute("data-theme", theme);
      try { localStorage.setItem(KEY, theme); } catch (e) {}
    }
    syncButton();
  }

  function syncButton() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var dark = current() === "dark";
    btn.setAttribute("data-current", dark ? "dark" : "light");
    btn.setAttribute("aria-pressed", dark ? "true" : "false");
  }

  // ?theme=dark|light 可强制主题（截图、嵌入、系统不支持深色的设备都用得上）
  function applyUrlParam() {
    var m = /[?&]theme=(dark|light)\b/.exec(window.location.search);
    if (m) {
      apply(m[1]);
      return true;
    }
    return false;
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyUrlParam();
    var btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.addEventListener("click", function () {
        apply(current() === "dark" ? "light" : "dark");
      });
    }
    syncButton();
  });

  // 未手动选择时，跟随系统主题变化
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var onChange = function () {
      if (!root.getAttribute("data-theme")) syncButton();
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }
})();
