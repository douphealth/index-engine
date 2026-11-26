// Applies the saved light/dark choice before the page paints (loaded without defer, so there is no flash).
// "auto" (nothing saved) follows the system setting through the stylesheet's media query.
(function () {
  var choice = null;
  try { choice = localStorage.getItem("ie-theme"); } catch (e) { /* private window: follow the system */ }
  if (choice === "light" || choice === "dark") document.documentElement.setAttribute("data-theme", choice);
})();
