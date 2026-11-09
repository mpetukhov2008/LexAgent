"use strict";
// Loaded before CSS: an explicit preference wins; the first visit is always dark.
(function () {
  let theme = 'dark';
  try {
    const saved = localStorage.getItem('dovod-theme');
    if (saved === 'light' || saved === 'dark') theme = saved;
  } catch { /* Storage can be disabled; the current tab still works. */ }
  document.documentElement.dataset.theme = theme;
})();
