// Applies the saved (or system) colour theme before the first paint so there is no light/dark flash.
// A separate file, not an inline script, because the Content-Security-Policy forbids inline scripts.
(function () {
  var mode = "system";
  try {
    var saved = localStorage.getItem("md_theme");
    if (saved === "light" || saved === "dark" || saved === "system") mode = saved;
  } catch (e) {
    /* storage blocked: follow the system */
  }
  var dark = mode === "dark" || (mode === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
})();
