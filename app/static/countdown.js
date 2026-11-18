// Counts down the seconds until the owner can try again (the page shows the number without JS too).
(function () {
  "use strict";
  document.querySelectorAll(".countdown[data-seconds]").forEach(function (el) {
    var left = parseInt(el.dataset.seconds, 10) || 0;
    var timer = setInterval(function () {
      left -= 1;
      if (left <= 0) { el.textContent = el.dataset.done; clearInterval(timer); return; }
      el.textContent = el.dataset.template.replace("{seconds}", String(left));
    }, 1000);
  });
})();
