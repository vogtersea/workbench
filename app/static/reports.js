/* 报告中心：按粒度切换表单字段。
   渐进增强：无 JS 时所有字段依然可见可提交，服务端只取与粒度相关的参数。 */
(function () {
  "use strict";

  function sync() {
    var kindSelect = document.getElementById("report-kind");
    if (!kindSelect) return;
    var value = kindSelect.value;

    var taskField = document.getElementById("field-task");
    if (taskField) taskField.classList.toggle("hidden", value !== "task");

    var rangeField = document.getElementById("field-range");
    if (rangeField) rangeField.classList.toggle("hidden", value !== "custom");
  }

  document.addEventListener("DOMContentLoaded", sync);
  document.addEventListener("change", function (event) {
    if (event.target && event.target.id === "report-kind") sync();
  });
})();
