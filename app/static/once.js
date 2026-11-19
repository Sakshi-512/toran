// Buttons marked data-once submit their form only once: after the first tap the button is
// disabled and shows a small ring with its data-once text ("Saving...", "Sending..."), so a
// double tap cannot send the form twice. Without JavaScript the form simply posts as before.
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
      if (!button.hasAttribute("data-label")) button.setAttribute("data-label", button.innerHTML);
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      if (button.dataset.once) {
        var ring = document.createElement("span");
        ring.className = "spinner spinner-small";
        ring.setAttribute("aria-hidden", "true");
        var label = document.createElement("span");
        label.textContent = button.dataset.once;
        button.replaceChildren(ring, label);
      }
    }, 0);
  });
  // Coming back (the back button, or a page restored from the browser's cache) re-enables it
  // with its own label again.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-sent]").forEach(function (form) { delete form.dataset.sent; });
    document.querySelectorAll("[data-once][aria-busy]").forEach(function (b) {
      b.disabled = false;
      b.removeAttribute("aria-busy");
      if (b.hasAttribute("data-label")) b.innerHTML = b.getAttribute("data-label");
    });
  });
})();
