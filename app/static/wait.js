// Waiting boxes (app/templates/app/_waiting.html): the page reloads itself while a job runs, so
// the time the wait started is kept in sessionStorage. Once a wait passes its job's own limit,
// the ring makes way for a calm note with one next step. Without storage or JavaScript, the
// page simply keeps waiting as before.
(function () {
  "use strict";
  var PREFIX = "toran-wait:";
  var boxes = document.querySelectorAll(".waiting[data-wait-limit]");
  var store = null;
  try { store = window.sessionStorage; store.getItem(PREFIX); } catch (e) { store = null; }
  if (!store) return;
  var present = {};
  boxes.forEach(function (box) { present[PREFIX + location.pathname + ":" + box.dataset.waitKey] = true; });
  // A wait that is no longer on its page has finished: forget it, so the next one starts fresh.
  try {
    for (var i = store.length - 1; i >= 0; i--) {
      var key = store.key(i);
      if (key && key.indexOf(PREFIX + location.pathname + ":") === 0 && !present[key]) store.removeItem(key);
    }
  } catch (e) { return; }
  boxes.forEach(function (box) {
    var key = PREFIX + location.pathname + ":" + box.dataset.waitKey;
    var limit = Number(box.dataset.waitLimit) * 1000;
    var since = Number(store.getItem(key));
    if (!since) { since = Date.now(); try { store.setItem(key, String(since)); } catch (e) { return; } }
    function late() {
      box.classList.add("is-late");
      var ring = box.querySelector(".spinner");
      if (ring) ring.hidden = true;
      var note = box.querySelector(".waiting-late");
      if (note) note.hidden = false;
    }
    var left = since + limit - Date.now();
    if (left <= 0) late(); else setTimeout(late, left);
  });
})();
