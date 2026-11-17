// Buttons marked data-once submit their form only once: after the first tap the button is
// disabled and shows its data-once text, so a double tap cannot send the form twice.
(function () {
  "use strict";
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var button = event.submitter;
    if (!button || !button.hasAttribute("data-once")) return;
    if (form.dataset.sent === "1") { event.preventDefault(); return; }
    form.dataset.sent = "1";
    // Disable after the browser has read the button's value for this submission.
    setTimeout(function () {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      if (button.dataset.once) button.textContent = button.dataset.once;
    }, 0);
  });
  // Coming back with the browser's back button re-enables it.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-sent]").forEach(function (form) {
      delete form.dataset.sent;
      form.querySelectorAll("[data-once]").forEach(function (b) { b.disabled = false; b.removeAttribute("aria-busy"); });
    });
  });
})();
