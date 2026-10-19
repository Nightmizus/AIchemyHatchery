(() => {
  const form = document.querySelector('.gallery-search');
  const input = document.querySelector('#gallery-search');
  const clear = form.querySelector('.search-clear');
  const status = document.querySelector('#search-status');
  const sections = [...document.querySelectorAll('[data-gallery-section]')];
  form.hidden = false;
  form.addEventListener('submit', event => event.preventDefault());
  function filter() {
    const query = input.value.trim().toLocaleLowerCase();
    let total = 0;
    for (const section of sections) {
      const cards = [...section.querySelectorAll('[data-gallery-card]')];
      let count = 0;
      for (const card of cards) {
        card.hidden = !card.dataset.search.toLocaleLowerCase().includes(query);
        if (!card.hidden) count += 1;
      }
      section.querySelector('[data-section-count]').textContent = count;
      section.querySelector('[data-empty-default]').hidden = Boolean(query) || cards.length > 0;
      section.querySelector('[data-empty-search]').hidden = !query || count > 0;
      total += count;
    }
    clear.hidden = !input.value;
    status.textContent = query ? `找到 ${total} 个匹配的网站` : `共收录 ${total} 个网站`;
  }
  input.addEventListener('input', filter);
  clear.addEventListener('click', () => { input.value = ''; filter(); input.focus(); });
  input.addEventListener('keydown', event => {
    if (event.key === 'Escape') { input.value = ''; filter(); }
  });
})();
