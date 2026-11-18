// Contraste — mejoras progresivas. La página funciona entera sin JavaScript;
// esto solo añade filtros en cliente. No hay peticiones de red ni almacenamiento.
(function () {
  "use strict";

  function initFilters() {
    var panel = document.querySelector("[data-filters]");
    var storiesRoot = document.querySelector("[data-stories]");
    if (!panel || !storiesRoot) return;
    panel.hidden = false;

    var pills = Array.prototype.slice.call(panel.querySelectorAll(".pill[data-pos]"));
    var groupSel = panel.querySelector("[data-group-filter]");
    var viewSel = panel.querySelector("[data-view]");
    var sortSel = panel.querySelector("[data-sort]");
    var countEl = panel.querySelector("[data-count]");
    var noMatch = document.querySelector("[data-no-match]");
    var activePos = "all";
    var flatRoot = null;

    function buildFlat() {
      flatRoot = document.createElement("div");
      flatRoot.className = "flat";
      var items = Array.prototype.slice.call(storiesRoot.querySelectorAll("[data-item]"));
      items.forEach(function (el, i) {
        var clone = el.cloneNode(true);
        clone.classList.add("single");
        clone.setAttribute("data-order", String(i));
        flatRoot.appendChild(clone);
      });
      storiesRoot.parentNode.insertBefore(flatRoot, storiesRoot.nextSibling);
    }

    function sortFlat() {
      if (!flatRoot) return;
      var items = Array.prototype.slice.call(flatRoot.children);
      var byTime = sortSel.value === "recent";
      items.sort(function (a, b) {
        if (byTime) return Number(b.getAttribute("data-time")) - Number(a.getAttribute("data-time"));
        return Number(a.getAttribute("data-order")) - Number(b.getAttribute("data-order"));
      });
      items.forEach(function (el) { flatRoot.appendChild(el); });
    }

    function sortStories() {
      var blocks = Array.prototype.slice.call(storiesRoot.children);
      blocks.forEach(function (b, i) { if (!b.hasAttribute("data-order")) b.setAttribute("data-order", String(i)); });
      var byTime = sortSel.value === "recent";
      function newest(b) {
        var times = Array.prototype.map.call(b.querySelectorAll("[data-time]"), function (x) { return Number(x.getAttribute("data-time")); });
        if (b.hasAttribute("data-time")) times.push(Number(b.getAttribute("data-time")));
        return Math.max.apply(null, times.concat([0]));
      }
      blocks.sort(function (a, b) {
        if (byTime) return newest(b) - newest(a);
        return Number(a.getAttribute("data-order")) - Number(b.getAttribute("data-order"));
      });
      blocks.forEach(function (b) { storiesRoot.appendChild(b); });
    }

    function matches(el) {
      var okPos = activePos === "all" || el.getAttribute("data-pos") === activePos;
      var okGroup = !groupSel.value || el.getAttribute("data-group") === groupSel.value;
      return okPos && okGroup;
    }

    function apply() {
      var flat = viewSel.value === "flat";
      if (flat && !flatRoot) buildFlat();
      storiesRoot.hidden = flat;
      if (flatRoot) flatRoot.hidden = !flat;
      if (flat) sortFlat(); else sortStories();

      var root = flat ? flatRoot : storiesRoot;
      var shown = 0;
      Array.prototype.forEach.call(root.querySelectorAll("[data-item]"), function (el) {
        var ok = matches(el);
        el.hidden = !ok;
        if (ok) shown++;
      });
      if (!flat) {
        Array.prototype.forEach.call(storiesRoot.querySelectorAll(".story"), function (story) {
          story.hidden = !story.querySelector("[data-item]:not([hidden])");
        });
      }
      countEl.textContent = shown === 1 ? "1 titular visible" : shown + " titulares visibles";
      noMatch.hidden = shown !== 0;
    }

    pills.forEach(function (pill) {
      pill.addEventListener("click", function () {
        activePos = pill.getAttribute("data-pos");
        pills.forEach(function (p) {
          var on = p === pill;
          p.classList.toggle("is-active", on);
          p.setAttribute("aria-pressed", on ? "true" : "false");
        });
        apply();
      });
    });
    [groupSel, viewSel, sortSel].forEach(function (s) { s.addEventListener("change", apply); });
    apply();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initFilters);
  else initFilters();
})();
