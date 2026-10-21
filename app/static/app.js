/* 工作志前端工具集：删除确认 / 回车提交 / 按钮 loading / 复制。
   无外链、无构建链，htmx 只负责渐进增强，所有变更在无 JS 时依然可用。 */
(function () {
  "use strict";

  function submitForm(form) {
    if (typeof form.requestSubmit === "function") {
      form.requestSubmit();
    } else {
      form.submit();
    }
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.top = "-1000px";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
        resolve();
      } catch (err) {
        reject(err);
      }
      document.body.removeChild(area);
    });
  }

  function resolveCopyText(trigger) {
    var selector = trigger.getAttribute("data-copy-target");
    if (selector) {
      var el = document.querySelector(selector);
      if (!el) return "";
      if (el.tagName === "TEXTAREA" || el.tagName === "INPUT") return el.value || "";
      return el.innerText || el.textContent || "";
    }
    return trigger.getAttribute("data-copy") || "";
  }

  /* 删除等危险操作二次确认 + 提交按钮 loading 态 */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.tagName !== "FORM") return;

    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }

    var button = form.querySelector("[data-loading-text]");
    if (button && !button.disabled) {
      button.setAttribute("data-original-text", button.textContent);
      button.textContent = button.getAttribute("data-loading-text");
      button.classList.add("is-loading");
      button.disabled = true;
    }
  });

  /* 浏览器后退 / bfcache 恢复时把按钮还原 */
  window.addEventListener("pageshow", function () {
    var buttons = document.querySelectorAll("[data-loading-text][disabled]");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      btn.disabled = false;
      btn.classList.remove("is-loading");
      var original = btn.getAttribute("data-original-text");
      if (original !== null) btn.textContent = original;
    }
  });

  /* 回车提交：单行输入框回车；文本域 Ctrl+Enter / Cmd+Enter */
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || event.isComposing) return;
    var target = event.target;
    if (!target || !target.tagName) return;

    var isTextarea = target.tagName === "TEXTAREA";
    var singleLineTypes = ["text", "search", "number", "password", "date", "time", "email", "url", "tel"];
    var isSingleLine =
      target.tagName === "INPUT" && singleLineTypes.indexOf((target.type || "text").toLowerCase()) >= 0;

    if (isTextarea) {
      if (!(event.ctrlKey || event.metaKey)) return;
    } else if (!isSingleLine) {
      return;
    }
    if (!target.form) return;
    event.preventDefault();
    submitForm(target.form);
  });

  /* 复制到剪贴板：[data-copy-target="#selector"] 或 [data-copy="文本"] */
  document.addEventListener("click", function (event) {
    var node = event.target;
    if (!node || typeof node.closest !== "function") return;
    var trigger = node.closest("[data-copy-target], [data-copy]");
    if (!trigger) return;

    var text = resolveCopyText(trigger);
    if (!text) return;
    event.preventDefault();

    var original = trigger.getAttribute("data-original-text") || trigger.textContent;
    copyText(text)
      .then(function () {
        trigger.textContent = "已复制";
        window.setTimeout(function () {
          trigger.textContent = original;
        }, 1500);
      })
      .catch(function () {
        window.alert("复制失败，请手动选择文本复制");
      });
  });
})();
