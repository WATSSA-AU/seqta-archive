(function () {
  "use strict";

  // Pastoral care filter (type + free text) on the student page
  var bar = document.querySelector(".js-pcfilter");
  if (bar) {
    var sel = bar.querySelector("select");
    var inp = bar.querySelector("input");
    var out = bar.querySelector(".js-pccount");
    var items = Array.prototype.slice.call(document.querySelectorAll(".js-pc"));
    var apply = function () {
      var t = sel.value, q = inp.value.trim().toLowerCase(), n = 0;
      items.forEach(function (el) {
        var ok = (!t || el.dataset.type === t) && (!q || el.textContent.toLowerCase().indexOf(q) >= 0);
        el.hidden = !ok;
        if (ok) n++;
      });
      out.textContent = (t || q) ? n + " of " + items.length + " shown" : "";
    };
    sel.addEventListener("change", apply);
    inp.addEventListener("input", apply);
  }

  // Load SEQTA message bodies only when opened
  document.querySelectorAll("details.js-msg").forEach(function (d) {
    d.addEventListener("toggle", function () {
      var f = d.querySelector("iframe");
      if (d.open && f && !f.getAttribute("src")) f.setAttribute("src", d.dataset.src);
    });
  });

  // Open a linked message when arriving via #msg-123
  if (location.hash.indexOf("#msg-") === 0) {
    var m = document.querySelector(location.hash);
    if (m && m.tagName === "DETAILS") { m.open = true; m.dispatchEvent(new Event("toggle")); }
  }

  // Report print button
  document.querySelectorAll(".js-print").forEach(function (b) {
    b.addEventListener("click", function () { window.print(); });
  });
})();
