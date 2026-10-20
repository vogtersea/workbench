/* 侧边栏抽屉：汉堡开关 / ESC 关闭 / 点击遮罩关闭。无依赖、无 CDN。 */
(function () {
  "use strict";

  function toggleButton() {
    return document.getElementById("sidebar-toggle");
  }

  function isOpen() {
    return document.body.classList.contains("drawer-open");
  }

  function setOpen(open) {
    document.body.classList.toggle("drawer-open", open);
    var button = toggleButton();
    if (button) {
      button.setAttribute("aria-expanded", open ? "true" : "false");
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || typeof target.closest !== "function") {
      return;
    }
    if (target.closest("#sidebar-toggle")) {
      setOpen(!isOpen());
      return;
    }
    if (target.closest("[data-sidebar-close]")) {
      setOpen(false);
      return;
    }
    if (isOpen() && !target.closest("#app-sidebar")) {
      setOpen(false);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isOpen()) {
      setOpen(false);
    }
  });
})();
