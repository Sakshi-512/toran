// After "Add a size or price" or "Add a product" the page opens at #new (the new row). Browsers
// skip autofocus when the address has a #fragment, and the jump to #new can clear an earlier
// focus or keep the old scroll position, so once the page has loaded, put the cursor in the new
// row's first field and bring it to the middle of the screen.
(function () {
  "use strict";
  if (location.hash !== "#new") return;
  window.addEventListener("load", function () {
    var field = document.querySelector("#new input:not([type=hidden])");
    if (!field) return;
    field.focus({ preventScroll: true });
    field.scrollIntoView({ block: "center" });
  });
})();
