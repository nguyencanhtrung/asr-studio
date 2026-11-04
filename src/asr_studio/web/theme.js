"use strict";

(() => {
  const storageKey = "asr-studio:theme";
  const root = document.documentElement;
  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const validPreferences = new Set(["system", "light", "dark"]);

  function storedPreference() {
    try {
      const value = window.localStorage.getItem(storageKey);
      return validPreferences.has(value) ? value : "system";
    } catch {
      return "system";
    }
  }

  function effectiveTheme(preference) {
    return preference === "system" ? (darkQuery.matches ? "dark" : "light") : preference;
  }

  function render(preference) {
    const safePreference = validPreferences.has(preference) ? preference : "system";
    root.dataset.themePreference = safePreference;
    root.dataset.theme = effectiveTheme(safePreference);
    const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
    const label = nextTheme === "light" ? t("Chuyển sang giao diện sáng") : t("Chuyển sang giao diện tối");
    for (const button of document.querySelectorAll("[data-theme-toggle]")) {
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
    }
    for (const input of document.querySelectorAll("[data-theme-choice]")) {
      input.checked = input.value === safePreference;
    }
  }

  function renderAtomically(preference) {
    root.dataset.themeChanging = "";
    render(preference);
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        delete root.dataset.themeChanging;
      });
    });
  }

  function setPreference(preference) {
    const safePreference = validPreferences.has(preference) ? preference : "system";
    try {
      if (safePreference === "system") window.localStorage.removeItem(storageKey);
      else window.localStorage.setItem(storageKey, safePreference);
    } catch {
      // The active tab still follows the chosen value when storage is unavailable.
    }
    renderAtomically(safePreference);
  }

  render(storedPreference());

  document.addEventListener("DOMContentLoaded", () => {
    render(storedPreference());
    for (const button of document.querySelectorAll("[data-theme-toggle]")) {
      button.addEventListener("click", () => {
        setPreference(root.dataset.theme === "dark" ? "light" : "dark");
      });
    }
    for (const input of document.querySelectorAll("[data-theme-choice]")) {
      input.addEventListener("change", () => setPreference(input.value));
    }
  });

  darkQuery.addEventListener("change", () => {
    if (root.dataset.themePreference === "system") renderAtomically("system");
  });

  window.addEventListener("storage", (event) => {
    if (event.key === storageKey) renderAtomically(storedPreference());
  });
})();
