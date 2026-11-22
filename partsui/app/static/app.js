// Parts Manager: the few behaviours htmx doesn't cover. No inline handlers (strict CSP).
(function () {
  "use strict";

  // Whole result rows are clickable; the part-number link stays for keyboard users.
  document.addEventListener("click", function (ev) {
    var row = ev.target.closest("tr[data-href]");
    if (row && !ev.target.closest("a, button, input, select, textarea")) {
      if (ev.ctrlKey || ev.metaKey) window.open(row.dataset.href, "_blank");
      else window.location.href = row.dataset.href;
    }
  });

  // Autocomplete: picking a suggestion fills the Ref field and its matching Path field.
  function pick(btn) {
    var ref = document.getElementById(btn.dataset.for);
    if (!ref) return;
    ref.value = btn.dataset.ref;
    var path = document.getElementById(ref.dataset.pathField);
    if (path) {
      path.value = btn.dataset.path;
      path.classList.add("filled");
      setTimeout(function () { path.classList.remove("filled"); }, 900);
    }
    var list = btn.closest(".suggest");
    if (list) list.innerHTML = "";
    ref.focus();
  }

  document.addEventListener("mousedown", function (ev) {
    var btn = ev.target.closest(".suggest-list button");
    if (btn) { ev.preventDefault(); pick(btn); }  // before the input loses focus
  });

  document.addEventListener("keydown", function (ev) {
    var t = ev.target;
    var box = t.closest && t.closest(".has-suggest");
    if (!box) return;
    var buttons = Array.prototype.slice.call(box.querySelectorAll(".suggest-list button"));
    if (ev.key === "Escape") { box.querySelector(".suggest").innerHTML = ""; return; }
    if (!buttons.length) return;
    var i = buttons.indexOf(document.activeElement);
    if (ev.key === "ArrowDown") { ev.preventDefault(); buttons[Math.min(i + 1, buttons.length - 1)].focus(); }
    else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      if (i <= 0) box.querySelector("input").focus(); else buttons[i - 1].focus();
    } else if (ev.key === "Enter" && i >= 0) { ev.preventDefault(); pick(buttons[i]); }
  });

  document.addEventListener("focusout", function (ev) {
    var box = ev.target.closest && ev.target.closest(".has-suggest");
    if (!box) return;
    setTimeout(function () {
      if (!box.contains(document.activeElement)) {
        var s = box.querySelector(".suggest");
        if (s) s.innerHTML = "";
      }
    }, 150);
  });
})();
