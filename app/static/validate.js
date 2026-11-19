// Live checks on the confirm page, My details and the menu editor. A progressive enhancement:
// the server stays the only authority, and with JavaScript off the forms post exactly as before.
// Each rule below mirrors a server rule; the file names the server code it copies.
(function () {
  "use strict";
  var box = document.getElementById("v-msgs");
  var form = document.querySelector("form[data-live]");
  if (!box || !form) return;
  var MSG = JSON.parse(box.getAttribute("data-msgs") || "{}");
  var PRICE_MAX = Number(box.getAttribute("data-price-max")) || 0;
  var page = form.getAttribute("data-live"); // confirm | details | menu
  var dirty = false;

  function text(key, values) {
    var out = MSG[key] || "";
    Object.keys(values || {}).forEach(function (k) { out = out.split("{" + k + "}").join(values[k]); });
    return out;
  }
  function chars(value) { return Array.from(value).length; } // like Python len(): code points

  // --- rules (each returns an i18n key and values, or null) ---------------------------------

  // app/services/field_rules.py business_name_problem, after draft_from_form strips the value.
  var NAME_PUNCT = "&'’-.,()/";
  function nameRule(raw) {
    var value = raw.trim();
    if (!value) return { key: "intake.name_needed" };
    var n = chars(value);
    if (n < 2 || n > 120) return { key: "intake.name_length" };
    var letter = false;
    for (var ch of value) {
      if (/\p{L}/u.test(ch)) letter = true;
      else if (/[\p{M}\p{Nd}]/u.test(ch) || ch === " " || ch === "‌" || ch === "‍") continue;
      else if (NAME_PUNCT.indexOf(ch) < 0) return { key: "intake.name_chars" };
    }
    return letter ? null : { key: "intake.name_letter" };
  }

  // app/services/web_address.py problem(), after the route trims and lower-cases the value.
  // Reserved and taken addresses are still checked by the server only.
  function addressRule(raw) {
    var value = raw.trim().toLowerCase();
    var n = chars(value);
    if (n < 3 || n > 40 || !/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(value) || value.indexOf("--") >= 0) {
      return { key: "intake.address_invalid" };
    }
    return null;
  }

  // app/schemas.py normalise_phone. The confirm page first turns Devanagari digits into 0-9
  // (draft_from_form uses ascii_digits); My details does not, so neither does this check there.
  var DEVANAGARI = "०१२३४५६७८९";
  function asciiDigits(value) {
    return value.replace(/[०-९]/g, function (d) { return String(DEVANAGARI.indexOf(d)); });
  }
  function phoneRule(raw) {
    var value = page === "confirm" ? asciiDigits(raw).trim() : raw.trim();
    if (chars(value) > 20) return { key: "intake.phone_invalid" };
    var digits = value.replace(/[\s\-().]/gu, "");
    if (!digits) return null;
    if (digits.indexOf("00") === 0) digits = "+" + digits.slice(2);
    if (digits[0] === "+") return /^\+\p{Nd}{10,15}$/u.test(digits) ? null : { key: "intake.phone_invalid" };
    if (digits[0] === "0" && chars(digits) === 11) digits = digits.slice(1);
    if (digits.indexOf("91") === 0 && chars(digits) === 12) digits = digits.slice(2);
    return /^[6-9]\p{Nd}{9}$/u.test(digits) ? null : { key: "intake.phone_invalid" };
  }

  // app/services/intake.py _price and _product_from_form, plus MAX_PRICE in app/schemas.py.
  function priceRule(raw, label) {
    var cleaned = asciiDigits(raw.trim()).split(",").join("").split("₹").join("").split(" ").join("").trim();
    if (!cleaned) return label.trim() ? { key: "intake.price_needed" } : null;
    if (!/^\p{Nd}+$/u.test(cleaned)) return { key: "intake.price_invalid" };
    if (/^[0-9]+$/.test(cleaned) && PRICE_MAX && Number(cleaned) > PRICE_MAX) {
      return { key: "v.price_high", values: { max: new Intl.NumberFormat("en-IN").format(PRICE_MAX) } };
    }
    return null;
  }

  // --- fields ----------------------------------------------------------------------------------

  var fields = Array.prototype.filter.call(
    form.querySelectorAll("input, textarea"),
    function (f) { return !/^(hidden|checkbox|radio|file|submit)$/.test(f.type) && !f.classList.contains("pin-input"); }
  );

  function kind(f) {
    if (f.name === "business_name") return "name";
    if (f.name === "web_address") return "address";
    if (f.name === "phone") return "phone";
    if (/^p\d+_o\d+_price$/.test(f.name)) return "price";
    if (/^p\d+_name$/.test(f.name)) return "product";
    return "text";
  }
  function cardRemoved(f) {
    var m = /^p(\d+)_/.exec(f.name);
    var box = m && form.querySelector('input[name="p' + m[1] + '_remove"]');
    return !!(box && box.checked);
  }
  function siblingValue(f, from, to) {
    var other = form.querySelector('[name="' + f.name.replace(from, to) + '"]');
    return other ? other.value : "";
  }
  function productHasRows(f) {
    var m = /^p(\d+)_name$/.exec(f.name);
    var rows = form.querySelectorAll('[name^="p' + m[1] + '_o"]');
    return Array.prototype.some.call(rows, function (r) {
      return /_(label|price)$/.test(r.name) && r.value.trim() !== "";
    });
  }

  // The problem with a field, or null. Length is checked for every field with a limit.
  function problem(f) {
    if (cardRemoved(f)) return null;
    var k = kind(f), max = Number(f.getAttribute("data-maxlength")) || 0;
    var rule = null;
    if (k === "name") rule = nameRule(f.value);
    else if (k === "address") rule = addressRule(f.value);
    else if (k === "phone") rule = phoneRule(f.value);
    else if (k === "price") rule = priceRule(f.value, siblingValue(f, /_price$/, "_label"));
    else if (k === "product" && !f.value.trim() && productHasRows(f)) rule = { key: "intake.name_needed" };
    if (rule) return rule;
    var n = chars(f.value.trim());
    if (max && n > max && k !== "phone" && k !== "price") return { key: "v.too_long", values: { max: max, n: n } };
    return null;
  }

  // --- messages under the field ----------------------------------------------------------------

  var seq = 0;
  function idFor(f) {
    if (!f.id) f.id = "v-" + f.name.replace(/[^a-z0-9_-]/gi, "-");
    return f.id;
  }
  function anchor(f) { return f.closest(".field-group, .option-row") || f; }
  function node(f, cls) {
    var id = idFor(f) + "-" + cls;
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement("p");
      el.id = id;
      el.className = cls;
      el.hidden = true;
      var after = anchor(f);
      var next = after.nextElementSibling;
      while (next && (next.classList.contains("live-msg") || next.classList.contains("field-count"))) {
        after = next;
        next = next.nextElementSibling;
      }
      after.insertAdjacentElement("afterend", el);
    }
    return el;
  }
  function describe(f) {
    var ids = ["live-msg", "field-count"].map(function (c) { return document.getElementById(idFor(f) + "-" + c); })
      .filter(function (el) { return el && !el.hidden; }).map(function (el) { return el.id; });
    if (ids.length) f.setAttribute("aria-describedby", ids.join(" "));
    else f.removeAttribute("aria-describedby");
  }
  function dropServerError(f) {
    // The server's message for this field is stale once the field is checked live.
    // It may follow a hint or a "read from your photo" note, but never another field.
    for (var next = anchor(f).nextElementSibling; next; next = next.nextElementSibling) {
      if (next.matches("label, input, select, textarea, button, .option-row, fieldset, div")) return;
      if (next.classList.contains("field-error") && !next.classList.contains("live-msg")) { next.remove(); return; }
    }
  }
  function showCount(f) {
    var max = Number(f.getAttribute("data-maxlength")) || 0;
    var k = kind(f);
    if (!max || k === "phone" || k === "price" || k === "address") return;
    var n = chars(f.value), el = node(f, "field-count");
    if (n >= max * 0.8 && n <= max) {
      el.textContent = n === max ? text("v.at_limit", { max: max }) : text("v.counter", { n: n, max: max });
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  }
  function show(f, rule) {
    var el = node(f, "live-msg");
    el.classList.add("field-error");
    if (rule) {
      el.textContent = text(rule.key, rule.values);
      el.hidden = false;
      f.setAttribute("aria-invalid", "true");
    } else {
      el.hidden = true;
      f.removeAttribute("aria-invalid");
    }
  }
  function check(f) {
    if (f.dataset.touched !== "1") { showCount(f); describe(f); return problem(f); }
    dropServerError(f);
    var rule = problem(f);
    show(f, rule);
    showCount(f);
    describe(f);
    return rule;
  }

  // --- wiring ----------------------------------------------------------------------------------

  form.noValidate = true; // our messages replace the browser's; the server still checks everything
  fields.forEach(function (f) {
    var max = f.getAttribute("maxlength");
    if (max) {
      // Keep pasted text whole and say it is too long, instead of cutting it off silently.
      f.setAttribute("data-maxlength", max);
      f.removeAttribute("maxlength");
    }
    f.addEventListener("blur", function () { f.dataset.touched = "1"; check(f); });
    f.addEventListener("input", function () {
      if (f.dataset.lettersBlocked === "1") { f.dataset.lettersBlocked = ""; }
      check(f);
    });
    if (kind(f) === "phone") {
      f.addEventListener("beforeinput", function (event) {
        // Letters are not taken while typing; digits (Latin or Devanagari), spaces and + - ( ) . are.
        if (event.inputType !== "insertText" || !event.data) return;
        if (/^[\p{Nd}\s+\-().]+$/u.test(event.data)) return;
        event.preventDefault();
        f.dataset.lettersBlocked = "1";
        var el = node(f, "live-msg");
        el.classList.add("field-error");
        el.textContent = text("v.phone_letters");
        el.hidden = false;
        describe(f);
      });
    }
  });
  // A product's name and its price rows depend on each other: recheck the whole card.
  form.addEventListener("input", function (event) {
    dirty = true;
    var m = /^p(\d+)_/.exec(event.target.name || "");
    if (!m) return;
    fields.forEach(function (f) { if (f !== event.target && f.name.indexOf("p" + m[1] + "_") === 0) check(f); });
  });
  form.addEventListener("change", function () { dirty = true; });

  function labelFor(f) {
    var label = f.id && form.querySelector('label[for="' + f.id + '"]');
    var name = label ? label.textContent : (f.getAttribute("aria-label") || f.name);
    return name.replace("*", "").trim();
  }
  function summary(bad) {
    var old = document.getElementById("v-summary");
    if (old) old.remove();
    if (!bad.length) return null;
    var wrap = document.createElement("div");
    wrap.id = "v-summary";
    wrap.className = "notice-error live-summary";
    wrap.setAttribute("role", "alert");
    wrap.tabIndex = -1;
    var head = document.createElement("p");
    head.className = "live-summary-title";
    head.textContent = bad.length === 1 ? text("v.summary_one") : text("v.summary", { n: bad.length });
    var list = document.createElement("ul");
    bad.forEach(function (item) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = "#" + idFor(item.field);
      var label = labelFor(item.field), message = text(item.rule.key, item.rule.values);
      a.textContent = message.indexOf(label) === 0 ? message : label + ": " + message;
      a.addEventListener("click", function (e) {
        e.preventDefault();
        item.field.focus({ preventScroll: true });
        item.field.scrollIntoView({ block: "center" });
      });
      li.appendChild(a);
      list.appendChild(li);
    });
    wrap.appendChild(head);
    wrap.appendChild(list);
    form.insertAdjacentElement("beforebegin", wrap);
    return wrap;
  }

  // Runs before once.js (capture on window), so a blocked Confirm never disables the button.
  window.addEventListener("submit", function (event) {
    if (event.target !== form) return;
    var button = event.submitter;
    var confirming = page === "confirm" && button && button.value === "confirm";
    if (!confirming) { dirty = false; return; } // Save changes and add-row buttons are never blocked
    var bad = [];
    fields.forEach(function (f) {
      f.dataset.touched = "1";
      var rule = check(f);
      if (rule) bad.push({ field: f, rule: rule });
    });
    if (!bad.length) { summary([]); dirty = false; return; }
    event.preventDefault();
    event.stopImmediatePropagation();
    summary(bad);
    bad[0].field.focus({ preventScroll: true });
    bad[0].field.scrollIntoView({ block: "center" });
  }, true);

  // Back link on the confirm page: ask only if something was edited since the page loaded
  // (Save changes and the add-row buttons store the draft, so a fresh page has nothing unsaved).
  if (page === "confirm") {
    var back = document.querySelector(".back-link");
    if (back) {
      back.addEventListener("click", function (event) {
        if (dirty && !window.confirm(text("v.leave"))) event.preventDefault();
      });
    }
  }
  window.addEventListener("pageshow", function (event) { if (event.persisted) dirty = false; });
})();
