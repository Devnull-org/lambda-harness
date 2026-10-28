"use strict";
(() => {
  const choices = ["system", "light", "tokyo-day", "dark"];
  let selected = "system";
  try {
    // Keep the appearance selected before the product was renamed.
    const saved =
      localStorage.getItem("lambda-harness-theme") ??
      localStorage.getItem("haskell-harness-theme");
    if (choices.includes(saved)) selected = saved;
  } catch {
    // The theme still works when browser storage is unavailable.
  }
  function apply(value) {
    selected = choices.includes(value) ? value : "system";
    document.documentElement.dataset.theme = selected;
    for (const picker of document.querySelectorAll("[data-theme-picker]"))
      picker.value = selected;
  }
  apply(selected);
  for (const picker of document.querySelectorAll("[data-theme-picker]"))
    picker.addEventListener("change", () => {
      apply(picker.value);
      try {
        localStorage.setItem("lambda-harness-theme", selected);
      } catch {
        // Keep the selection for this page even if it cannot be saved.
      }
    });
})();
