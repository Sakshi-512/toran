// Pages waiting for a background job reload themselves every few seconds.
// location.reload() always reloads, even when the address ends in a #fragment (a plain
// <meta http-equiv="refresh"> to the same address does not), and keeps the scroll position.
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="auto-refresh"]');
  var seconds = meta ? parseInt(meta.content, 10) : 0;
  if (seconds > 0) {
    setTimeout(function () { window.location.reload(); }, seconds * 1000);
  }
})();
