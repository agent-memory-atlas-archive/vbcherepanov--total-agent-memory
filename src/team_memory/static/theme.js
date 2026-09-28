'use strict';
(() => {
  let stored = null;
  try { stored = window.localStorage.getItem('tam-theme'); } catch (error) { stored = null; }
  const system = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  document.documentElement.dataset.theme = stored === 'light' || stored === 'dark' ? stored : system;
})();
