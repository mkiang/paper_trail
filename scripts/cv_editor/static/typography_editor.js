// Typography editor wiring: color-picker <-> hex text sync, per-knob reset, Cmd/Ctrl+S.
// No per-page injected data needed — operates on data-attributes/classes.
(function () {
  "use strict";

  function isHex(v) {
    return /^#?[0-9A-Fa-f]{6}$/.test(v.trim());
  }
  function normHex(v) {
    return "#" + v.trim().replace(/^#/, "").toUpperCase();
  }

  // color picker -> text field
  document.querySelectorAll(".ty-color").forEach(function (picker) {
    var text = document.getElementById(picker.dataset.target);
    if (!text) return;
    picker.addEventListener("input", function () {
      text.value = normHex(picker.value);
    });
    // text field -> color picker (only when it parses as hex)
    text.addEventListener("input", function () {
      if (isHex(text.value)) picker.value = normHex(text.value);
    });
  });

  // reset to default
  document.querySelectorAll(".ty-reset").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = document.getElementById(btn.dataset.target);
      if (!text) return;
      text.value = text.dataset.default || "";
      text.dispatchEvent(new Event("input", { bubbles: true }));
    });
  });

  // Cmd/Ctrl+S submits
  var form = document.getElementById("ty-form");
  if (form) {
    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        form.submit();
      }
    });
  }
})();
