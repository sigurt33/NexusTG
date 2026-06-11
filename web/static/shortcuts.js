// Клавиатурные шорткаты для инбокса.
(function(){
  let idx = -1;

  function rows() {
    return Array.from(document.querySelectorAll('#message-list .msg-row'));
  }
  function clearFocus() {
    rows().forEach(r => r.classList.remove('kbd-focus'));
  }
  function focusRow(i) {
    const r = rows();
    if (!r.length) return;
    idx = Math.max(0, Math.min(r.length - 1, i));
    clearFocus();
    r[idx].classList.add('kbd-focus');
    r[idx].scrollIntoView({block:'nearest', behavior:'smooth'});
  }
  function current() {
    const r = rows();
    if (idx < 0 || idx >= r.length) return null;
    return r[idx];
  }
  function clickIn(el, selector) {
    const b = el.querySelector(selector);
    if (b) b.click();
  }

  function updateCount() {
    const c = document.getElementById('msg-count');
    if (c) c.textContent = rows().length;
  }

  // re-arm focus + пересчёт счётчика после HTMX swap
  document.body.addEventListener('htmx:afterSwap', () => {
    const r = rows();
    if (idx >= r.length) idx = r.length - 1;
    if (idx >= 0) focusRow(idx);
    updateCount();
  });

  document.addEventListener('keydown', (e) => {
    // не мешать вводу в текстовых полях
    const tag = (e.target && e.target.tagName || '').toLowerCase();
    if (['input','textarea','select'].includes(tag)) {
      // / в любом месте — фокус на поиск (см. ниже)
      if (e.key === 'Escape' && tag === 'input') { e.target.blur(); }
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    switch (e.key) {
      case 'j': // вниз
        e.preventDefault();
        focusRow(idx < 0 ? 0 : idx + 1);
        break;
      case 'k': // вверх
        e.preventDefault();
        focusRow(idx < 0 ? 0 : idx - 1);
        break;
      case 'g': // в начало
        e.preventDefault();
        focusRow(0);
        break;
      case 'G': // в конец
        e.preventDefault();
        focusRow(rows().length - 1);
        break;
      case 'e': // готово
        if (current()) { e.preventDefault(); clickIn(current(), 'button[hx-post$="/done"]'); }
        break;
      case 's': // отложить 1 час
        if (current()) {
          e.preventDefault();
          clickIn(current(), 'button[hx-vals*=\'"1h"\']');
        }
        break;
      case 'a': // архив
        if (current()) { e.preventDefault(); clickIn(current(), 'button[hx-post$="/archive"]'); }
        break;
      case 'o': // открыть карточку
        if (current()) {
          e.preventDefault();
          const a = current().querySelector('a.msg-link');
          if (a) window.location.href = a.getAttribute('href');
        }
        break;
      case 't': // открыть в TG (deep-link с детальной)
        if (current()) {
          e.preventDefault();
          const a = current().querySelector('a.msg-link');
          if (a) window.open(a.getAttribute('href'), '_blank');
        }
        break;
      case '/':
        e.preventDefault();
        const q = document.getElementById('q') || document.querySelector('input[name="q"]');
        if (q) { q.focus(); q.select && q.select(); }
        break;
      case '?':
        e.preventDefault();
        const help = document.getElementById('kbd-help');
        if (help) help.open = !help.open;
        break;
    }
  });
})();
