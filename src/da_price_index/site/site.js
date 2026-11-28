// DAPriceScraper public site: tabs, copy buttons, base URL, docs nav, live prices.
(function () {
  "use strict";

  var BASE = /^https?:$/.test(location.protocol) ? location.origin : "https://your-host";
  var LANG_KEY = "dps-lang";

  // ---- Base URL in every code sample -------------------------------------
  document.querySelectorAll("[data-base]").forEach(function (el) {
    el.textContent = BASE;
  });

  // ---- Tabs (language choice is shared across every tab group) -----------
  function selectLang(lang, save) {
    document.querySelectorAll("[data-tabs]").forEach(function (group) {
      var tabs = group.querySelectorAll(".tab");
      var has = Array.prototype.some.call(tabs, function (t) { return t.dataset.lang === lang; });
      if (!has) return;
      tabs.forEach(function (t) {
        var on = t.dataset.lang === lang;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
      });
      group.querySelectorAll(".panel").forEach(function (p) {
        p.hidden = p.dataset.lang !== lang;
      });
    });
    if (save) {
      try { localStorage.setItem(LANG_KEY, lang); } catch (e) { /* storage unavailable */ }
    }
  }

  document.querySelectorAll("[data-tabs] .tab").forEach(function (tab) {
    tab.addEventListener("click", function () { selectLang(tab.dataset.lang, true); });
    tab.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      var tabs = Array.prototype.slice.call(tab.parentNode.querySelectorAll(".tab"));
      var i = tabs.indexOf(tab) + (e.key === "ArrowRight" ? 1 : -1);
      var next = tabs[(i + tabs.length) % tabs.length];
      selectLang(next.dataset.lang, true);
      next.focus();
    });
  });

  var savedLang = null;
  try { savedLang = localStorage.getItem(LANG_KEY); } catch (e) { /* storage unavailable */ }
  if (savedLang) selectLang(savedLang, false);

  // ---- Copy buttons -------------------------------------------------------
  document.querySelectorAll(".copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var card = btn.closest(".code-card");
      var pre = card.querySelector(".panel:not([hidden]) pre") || card.querySelector("pre");
      var text = "";
      if (pre) {
        // Copy the code only: drop instruction lines marked data-nocopy.
        var clone = pre.cloneNode(true);
        clone.querySelectorAll("[data-nocopy]").forEach(function (el) { el.remove(); });
        text = clone.textContent.replace(/^\n+|\n+$/g, "");
      }
      var done = function () {
        btn.textContent = "Copied";
        btn.classList.add("copied");
        setTimeout(function () { btn.textContent = "Copy"; btn.classList.remove("copied"); }, 1600);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () {});
      } else {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); done(); } catch (e) { /* ignore */ }
        document.body.removeChild(ta);
      }
    });
  });

  // ---- Docs nav: collapsed on narrow screens, highlights the section in view
  var docsNav = document.querySelector("details.docs-nav");
  if (docsNav && window.matchMedia("(max-width: 959px)").matches) {
    docsNav.open = false;
    docsNav.addEventListener("click", function (e) {
      if (e.target.closest("a")) docsNav.open = false;
    });
  }
  var navLinks = document.querySelectorAll(".docs-nav a[href^='#']");
  if (navLinks.length && "IntersectionObserver" in window) {
    var byId = {};
    navLinks.forEach(function (a) { byId[a.getAttribute("href").slice(1)] = a; });
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var link = byId[entry.target.id];
        if (!link) return;
        navLinks.forEach(function (a) { a.classList.remove("active"); });
        link.classList.add("active");
      });
    }, { rootMargin: "-80px 0px -70% 0px" });
    Object.keys(byId).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) observer.observe(el);
    });
  }

  // ---- Landing page: live stats and price explorer ------------------------
  var explorer = document.getElementById("explorer");
  if (!explorer) return;

  var els = {
    search: document.getElementById("ex-search"),
    category: document.getElementById("ex-category"),
    body: document.getElementById("ex-body"),
    empty: document.getElementById("ex-empty"),
    count: document.getElementById("ex-count"),
    asOf: document.getElementById("ex-asof"),
    statDate: document.getElementById("stat-date"),
    statCommodities: document.getElementById("stat-commodities"),
    statCategories: document.getElementById("stat-categories"),
    statMarkets: document.getElementById("stat-markets"),
  };
  var items = [];

  function titleCase(s) {
    return String(s || "").toLowerCase().replace(/(^|[\s(\/-])([a-z])/g, function (m, p, c) {
      return p + c.toUpperCase();
    });
  }
  function formatDate(iso) {
    var d = new Date(iso + "T00:00:00");
    if (isNaN(d)) return iso;
    return d.toLocaleDateString("en-PH", { month: "short", day: "numeric", year: "numeric" });
  }
  function formatPrice(v) {
    var n = Number(v);
    if (v === null || isNaN(n)) return "n/a";
    return "₱" + n.toLocaleString("en-PH", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function td(text, cls) {
    var cell = document.createElement("td");
    cell.textContent = text;
    if (cls) cell.className = cls;
    return cell;
  }

  function render() {
    var q = els.search.value.trim().toLowerCase();
    var cat = els.category.value;
    var rows = items.filter(function (it) {
      if (cat && it.top_category !== cat) return false;
      if (!q) return true;
      return (it.commodity_name + " " + it.specification + " " + (it.sub_category || ""))
        .toLowerCase().indexOf(q) !== -1;
    });
    var frag = document.createDocumentFragment();
    rows.forEach(function (it) {
      var tr = document.createElement("tr");
      var name = td(it.commodity_name);
      tr.appendChild(name);
      tr.appendChild(td(it.specification || "—", "spec col-spec"));
      tr.appendChild(td(titleCase(it.sub_category || it.top_category), "cat col-cat"));
      tr.appendChild(td(formatPrice(it.price_prevailing) + " / " + it.unit, "num"));
      frag.appendChild(tr);
    });
    els.body.replaceChildren(frag);
    els.empty.hidden = rows.length !== 0;
    els.count.textContent = rows.length + " of " + items.length + " commodities";
  }

  function setStat(el, value) { if (el) el.textContent = value; }

  function fail(message) {
    els.empty.hidden = false;
    els.empty.textContent = message;
    els.count.textContent = "";
    ["statDate", "statCommodities", "statCategories", "statMarkets"].forEach(function (k) {
      if (els[k] && !els[k].textContent.trim()) els[k].textContent = "—";
    });
  }

  fetch(BASE + "/prices/latest?available_only=true", { headers: { Accept: "application/json" } })
    .then(function (res) {
      if (res.status === 404) throw new Error("no-data");
      if (!res.ok) throw new Error("http-" + res.status);
      return res.json();
    })
    .then(function (data) {
      items = data.items || [];
      var cats = [];
      items.forEach(function (it) { if (cats.indexOf(it.top_category) === -1) cats.push(it.top_category); });
      cats.sort().forEach(function (c) {
        var opt = document.createElement("option");
        opt.value = c;
        opt.textContent = titleCase(c);
        els.category.appendChild(opt);
      });
      setStat(els.statDate, formatDate(data.as_of_date));
      setStat(els.statCommodities, String(data.total_commodities));
      setStat(els.statCategories, String(cats.length));
      els.asOf.textContent = "As of " + formatDate(data.as_of_date) + " · " + data.region_code;
      render();
      return fetch(BASE + "/reports/" + data.as_of_date).then(function (r) { return r.ok ? r.json() : null; });
    })
    .then(function (report) {
      setStat(els.statMarkets, report ? String(report.covered_markets_cnt) : "—");
    })
    .catch(function (err) {
      fail(err && err.message === "no-data"
        ? "No reports have been synced yet. Run da-dpi sync to load data."
        : "Couldn't load live prices right now. The API docs still work.");
    });

  els.search.addEventListener("input", render);
  els.category.addEventListener("change", render);
})();
