"use strict";

(() => {
  const storageKey = "asr-extension-production-v1:theme";
  const choices = ["system", "light", "dark"];
  const root = document.documentElement;
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

  function readPreference() {
    try {
      const value = window.localStorage.getItem(storageKey);
      return choices.includes(value) ? value : "system";
    } catch {
      return "system";
    }
  }

  function effectiveTheme(preference) {
    return preference === "system" ? (darkQuery.matches ? "dark" : "light") : preference;
  }

  function render(preference) {
    const safePreference = choices.includes(preference) ? preference : "system";
    const activeTheme = effectiveTheme(safePreference);
    root.dataset.themePreference = safePreference;
    root.dataset.theme = activeTheme;
    const nextPreference = choices[(choices.indexOf(safePreference) + 1) % choices.length];
    const labels = {
      system: t("Đổi giao diện · đang theo hệ thống"),
      light: t("Đổi giao diện · sáng"),
      dark: t("Đổi giao diện · tối")
    };
    const label = labels[nextPreference];
    for (const button of document.querySelectorAll("[data-theme-toggle]")) {
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
      button.dataset.themePreference = safePreference;
    }
  }

  function setPreference(preference) {
    const safePreference = choices.includes(preference) ? preference : "system";
    try {
      if (safePreference === "system") window.localStorage.removeItem(storageKey);
      else window.localStorage.setItem(storageKey, safePreference);
    } catch {
      // The extension page still follows the chosen value for this render.
    }
    root.dataset.themeChanging = "";
    render(safePreference);
    window.requestAnimationFrame(() => delete root.dataset.themeChanging);
  }

  onLanguageChange(() => render(readPreference()));
  render(readPreference());

  window.addEventListener("DOMContentLoaded", () => {
    render(readPreference());
    for (const button of document.querySelectorAll("[data-theme-toggle]")) {
      button.addEventListener("click", () => {
        const current = root.dataset.themePreference || "system";
        setPreference(choices[(choices.indexOf(current) + 1) % choices.length]);
      });
    }
  });

  const onSystemThemeChange = () => {
    if (root.dataset.themePreference === "system") render("system");
  };
  if (typeof darkQuery.addEventListener === "function") darkQuery.addEventListener("change", onSystemThemeChange);
  else darkQuery.addListener(onSystemThemeChange);
})();
