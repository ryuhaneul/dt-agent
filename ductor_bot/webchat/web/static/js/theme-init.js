(() => {
  let preference = 'system';
  try {
    const saved = localStorage.getItem('dtw-theme');
    if (saved === 'dark' || saved === 'light') preference = saved;
  } catch { /* The system preference also works when storage is unavailable. */ }
  const root = document.documentElement;
  root.dataset.themePref = preference;
  root.dataset.theme = preference === 'system'
    ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : preference;
})();
