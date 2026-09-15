// Имя цели не лежит в HTML: приезжает по нажатию и стирается при скрытии.
(function () {
  'use strict';

  const box = document.getElementById('targetBox');
  if (box) {
    const name = document.getElementById('targetName');
    const sub = document.getElementById('targetSub');
    const bar = box.querySelector('.bar');
    let timer = null;

    function hide() {
      clearTimeout(timer);
      box.classList.remove('open');
      setTimeout(() => { name.textContent = ''; sub.textContent = ''; }, 250);
    }

    bar.addEventListener('click', async () => {
      if (box.classList.contains('open')) { hide(); return; }
      const body = new FormData();
      body.append('csrf', box.dataset.csrf);
      try {
        const res = await fetch(box.dataset.url, { method: 'POST', body });
        const data = await res.json();
        name.textContent = res.ok ? data.target : (data.error || 'Недоступно');
        sub.textContent = res.ok ? 'нажмите, чтобы скрыть' : '';
      } catch (e) {
        name.textContent = 'Сервер не ответил';
      }
      box.classList.add('open');
      timer = setTimeout(hide, 20000);
    });
    box.addEventListener('click', (e) => {
      if (box.classList.contains('open') && e.target !== bar) hide();
    });
  }

  // Выбор цвета: показываем выбранное в заголовке и закрываем список.
  const colorPick = document.getElementById('colorPick');
  if (colorPick) {
    const dot = colorPick.querySelector('summary .dot-color');
    const name = document.getElementById('colorName');
    colorPick.addEventListener('change', (e) => {
      const input = e.target;
      if (!input.name || input.name !== 'color') return;
      dot.style.background = input.value;
      name.textContent = input.dataset.name;
      colorPick.open = false;
    });
    document.addEventListener('click', (e) => {
      if (colorPick.open && !colorPick.contains(e.target)) colorPick.open = false;
    });
  }

  // Список набора: поиск и счётчик отмеченных.
  const picker = document.getElementById('picker');
  if (picker) {
    const boxes = () => picker.querySelectorAll('input[name="user_ids"]');
    const counter = document.getElementById('pickCount');
    const search = document.getElementById('pickSearch');
    const all = document.getElementById('checkAll');

    function recount() {
      const n = picker.querySelectorAll('input[name="user_ids"]:checked').length;
      if (counter) counter.textContent = n ? 'отмечено ' + n : '';
    }
    picker.addEventListener('change', recount);
    if (all) all.addEventListener('change', () => {
      boxes().forEach((cb) => {
        if (cb.closest('label').hidden) return;
        cb.checked = all.checked;
      });
      recount();
    });
    if (search) search.addEventListener('input', () => {
      const q = search.value.trim().toLowerCase();
      picker.querySelectorAll('label[data-name]').forEach((row) => {
        row.hidden = q && !row.dataset.name.includes(q);
      });
    });
    recount();
  }

  // Пуш по http недоступен — страница игры сама спрашивает, не изменилось ли что.
  const root = document.getElementById('gameRoot');
  if (root && root.dataset.stateUrl) {
    const known = { alive: root.dataset.alive, claim: root.dataset.claim };
    setInterval(async () => {
      if (document.hidden) return;
      try {
        const res = await fetch(root.dataset.stateUrl);
        if (!res.ok) return;
        const data = await res.json();
        const claim = data.claim ? String(data.claim) : '';
        if (String(data.alive) !== known.alive || claim !== known.claim) location.reload();
      } catch (e) { /* сеть моргнула */ }
    }, 20000);
  }

  // navigator.clipboard по http недоступен — работает только старый путь.
  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const ta = document.createElement('textarea');
      ta.value = btn.dataset.copy;
      ta.style.cssText = 'position:fixed;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); btn.textContent = 'Скопировано'; }
      catch (e) { btn.textContent = 'Скопируйте вручную'; }
      ta.remove();
    });
  });
})();
