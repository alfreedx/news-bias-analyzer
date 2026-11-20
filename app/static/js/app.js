// Contraste — mejoras progresivas. La página funciona entera sin JavaScript;
// esto solo añade filtros en cliente y sugerencias al escribir (peticiones solo a este mismo servidor;
// no se usa el almacenamiento del navegador).
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
    // The «no match» message does not exist in the HTML: it is added only while the filters hide everything.
    var noMatch = document.createElement("p");
    noMatch.className = "empty";
    noMatch.setAttribute("data-no-match", "");
    noMatch.textContent = "Ningún titular coincide con los filtros.";
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
      var fold = storiesRoot.querySelector(":scope > details[data-more]");
      var blocks = Array.prototype.slice.call(storiesRoot.children).filter(function (b) { return b !== fold; });
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
      if (fold) storiesRoot.appendChild(fold);
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
      var more = storiesRoot.querySelector(":scope > details[data-more]");
      if (more) {
        var inFold = more.querySelectorAll("[data-item]:not([hidden])").length;
        more.hidden = !flat && !inFold;
        // the filters left nothing outside the fold: open it so the visible headlines are not hidden away
        if (inFold && shown === inFold) more.open = true;
      }
      countEl.textContent = shown === 1 ? "1 titular visible" : shown + " titulares visibles";
      if (shown === 0) {
        if (!noMatch.parentNode) storiesRoot.parentNode.insertBefore(noMatch, storiesRoot.nextSibling);
      } else if (noMatch.parentNode) {
        noMatch.parentNode.removeChild(noMatch);
      }
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

  function initTableFilters() {
    var box = document.querySelector("[data-table-filters]");
    if (!box) return;
    box.hidden = false;
    var selects = Array.prototype.slice.call(box.querySelectorAll("select[data-filter]"));
    var countEl = box.querySelector("[data-table-count]");
    var rows = Array.prototype.slice.call(document.querySelectorAll("tr[data-row]"));
    function apply() {
      var shown = 0;
      rows.forEach(function (row) {
        var ok = selects.every(function (sel) {
          if (!sel.value) return true;
          var values = (row.getAttribute("data-" + sel.getAttribute("data-filter")) || "").split("|");
          return values.indexOf(sel.value) !== -1;
        });
        row.hidden = !ok;
        if (ok) shown++;
      });
      countEl.textContent = shown === 1 ? "1 medio" : shown + " medios";
    }
    selects.forEach(function (sel) { sel.addEventListener("change", apply); });
    apply();
  }

  // Sugerencias al escribir: combobox ARIA 1.2 con lista (listbox) bajo el buscador.
  function initSuggestions() {
    if (!window.fetch || !window.AbortController) return;
    var seq = 0;
    Array.prototype.forEach.call(document.querySelectorAll("form[role=search] input[name=q]"), function (input) {
      var form = input.form;
      var id = "sug-" + (++seq);
      var list = document.createElement("ul");
      list.id = id;
      list.className = "sug";
      list.setAttribute("role", "listbox");
      list.setAttribute("aria-label", "Historias parecidas");
      list.hidden = true;
      var status = document.createElement("div");
      status.className = "visually-hidden";
      status.setAttribute("role", "status");
      form.classList.add("has-sug");
      form.appendChild(list);
      form.appendChild(status);
      input.setAttribute("role", "combobox");
      input.setAttribute("aria-autocomplete", "list");
      input.setAttribute("aria-expanded", "false");
      input.setAttribute("aria-controls", id);

      var items = [];
      var active = -1;
      var timer = null;
      var controller = null;

      function close() {
        list.hidden = true;
        input.setAttribute("aria-expanded", "false");
        input.removeAttribute("aria-activedescendant");
        active = -1;
      }
      function setActive(i) {
        var nodes = list.children;
        if (active >= 0 && nodes[active]) nodes[active].setAttribute("aria-selected", "false");
        active = i;
        if (i >= 0 && nodes[i]) {
          nodes[i].setAttribute("aria-selected", "true");
          input.setAttribute("aria-activedescendant", nodes[i].id);
          if (nodes[i].scrollIntoView) nodes[i].scrollIntoView({ block: "nearest" });
        } else {
          input.removeAttribute("aria-activedescendant");
        }
      }
      function go(i) {
        var url = items[i] && items[i].url;
        if (typeof url === "string" && url.charAt(0) === "/" && url.charAt(1) !== "/") window.location.href = url;
      }
      function render(data) {
        items = data.slice(0, 6);
        while (list.firstChild) list.removeChild(list.firstChild);
        active = -1;
        input.removeAttribute("aria-activedescendant");
        if (!items.length) { close(); status.textContent = ""; return; }
        items.forEach(function (it, i) {
          var li = document.createElement("li");
          li.id = id + "-" + i;
          li.className = "sug__item";
          li.setAttribute("role", "option");
          li.setAttribute("aria-selected", "false");
          var t = document.createElement("span");
          t.className = "sug__title";
          t.textContent = it.title;
          var m = document.createElement("span");
          m.className = "sug__meta";
          m.textContent = typeof it.outlets === "number"
            ? it.outlets + (it.outlets === 1 ? " medio" : " medios") + " · " + it.when
            : it.outlet + " · titular";
          li.appendChild(t);
          li.appendChild(m);
          li.addEventListener("click", function () { go(i); });
          list.appendChild(li);
        });
        list.hidden = false;
        input.setAttribute("aria-expanded", "true");
        status.textContent = items.length === 1 ? "1 sugerencia" : items.length + " sugerencias";
      }
      function fetchNow() {
        var q = input.value.trim();
        if (controller) controller.abort();
        if (q.length < 3) { controller = null; close(); status.textContent = ""; return; }
        controller = new AbortController();
        var mine = controller;
        fetch("/api/sugerencias?q=" + encodeURIComponent(q.slice(0, 100)), {
          signal: mine.signal, credentials: "omit", cache: "no-store", headers: { Accept: "application/json" }
        }).then(function (r) { return r.ok ? r.json() : { suggestions: [] }; })
          .then(function (d) { if (controller === mine) render(d.suggestions || []); })
          .catch(function () { if (controller === mine) close(); });
      }

      input.addEventListener("input", function () {
        clearTimeout(timer);
        if (input.value.trim().length < 3) { fetchNow(); return; }
        timer = setTimeout(fetchNow, 250);
      });
      input.addEventListener("keydown", function (e) {
        var open = !list.hidden;
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          if (!items.length) return;
          e.preventDefault();
          if (!open) { list.hidden = false; input.setAttribute("aria-expanded", "true"); }
          var n = items.length;
          setActive(e.key === "ArrowDown" ? (active + 1) % n : (active <= 0 ? n - 1 : active - 1));
        } else if (e.key === "Enter") {
          if (open && active >= 0) { e.preventDefault(); go(active); }
        } else if (e.key === "Escape") {
          if (open) { e.preventDefault(); close(); }
        }
      });
      input.addEventListener("blur", function () { clearTimeout(timer); close(); });
      list.addEventListener("mousedown", function (e) { e.preventDefault(); });
      form.addEventListener("submit", function () {
        clearTimeout(timer);
        if (controller) controller.abort();
        controller = null;
        close();
      });
      document.addEventListener("click", function (e) { if (!form.contains(e.target)) close(); });
    });
  }

  function init() { initFilters(); initTableFilters(); initSuggestions(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
