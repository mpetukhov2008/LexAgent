"use strict";
(function () {
  const supportTheme = document.querySelector('meta[name="theme-color"]');
  const themeButtons = [...document.querySelectorAll('.theme-toggle')];
  function applyTheme(theme, remember = false) {
    document.documentElement.dataset.theme = theme;
    supportTheme.content = theme === 'dark' ? '#0c111c' : '#f2f5fa';
    for (const button of themeButtons) {
      button.textContent = theme === 'dark' ? '☀  Светлая тема' : '☾  Тёмная тема';
      button.setAttribute('aria-label', theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему');
      button.setAttribute('aria-pressed', String(theme === 'light'));
    }
    if (remember) try {localStorage.setItem('dovod-theme', theme);} catch { /* Optional. */ }
  }
  applyTheme(document.documentElement.dataset.theme || 'dark');
  themeButtons.forEach(button => {button.onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark', true);});
  function enter() {
    $('welcome-screen').hidden = true; $('app-shell').hidden = false;
    document.body.classList.remove('welcome-active');
    try {sessionStorage.setItem('dovod-entered', '1');} catch { /* Optional. */ }
    if (location.hash === '#welcome') location.hash = '';
    window.scrollTo({top: 0, behavior: 'instant'});
  }
  function welcome() {
    $('welcome-screen').hidden = false; $('app-shell').hidden = true;
    document.body.classList.add('welcome-active'); location.hash = 'welcome';
    $('enter-workspace').focus({preventScroll: true});
  }
  window.openDovodWorkspace = enter; window.openDovodWelcome = welcome;
  $('enter-workspace').onclick = () => {enter(); $('title').focus({preventScroll: true});};
  $('try-example').onclick = () => {enter(); document.querySelector('[data-sample="complete"]').click(); $('workspace').scrollIntoView({behavior: 'smooth'});};
  $('back-to-welcome').onclick = welcome;
  let entered = false;
  try {entered = sessionStorage.getItem('dovod-entered') === '1';} catch { /* Optional. */ }
  if (location.hash !== '#welcome' && (entered || location.hash.length > 1)) enter();
  else document.body.classList.add('welcome-active');
  document.addEventListener('click', event => {
    const target = event.target.closest('button, .support-link, .sidebar-support');
    if (!target || target.disabled || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    target.classList.remove('tap-feedback'); void target.offsetWidth; target.classList.add('tap-feedback');
    target.addEventListener('animationend', () => target.classList.remove('tap-feedback'), {once: true});
  });
  const {agents, derive, eventStage} = window.DovodProgress;
  const agentById = Object.fromEntries(agents.map(a => [a.id, a]));
  const stageButtons = [...document.querySelectorAll('#agent-steps button')];
  const words = {waiting: 'Ожидает', running: 'Работает', done: 'Готово', attention: 'Внимание', skipped: 'Не выполнялся', stopped: 'Остановлен', error: 'Ошибка'};
  const toolWords = {
    list_documents: 'Собирает исходные материалы', read_document: 'Читает описание и документы',
    record_facts: 'Фиксирует сведения с цитатами', ask_clarification: 'Готовит уточняющий вопрос',
    finish: 'Завершает карточку ситуации', inspect_context: 'Изучает материалы этапа',
    inspect_cases: 'Изучает карточки судебных актов', read_evidence: 'Читает цитаты из источников',
    record_comparisons: 'Сохраняет сравнение обстоятельств', finish_comparison: 'Завершает сопоставление',
    get_verified_materials: 'Собирает проверенные основания', record_conclusion: 'Формирует вывод со ссылками',
    finish_report: 'Сохраняет исследовательскую записку', request_research: 'Запрашивает дополнительные материалы'
  };
  let current = null, events = [], inspected = null, lastActive = null, ticker = null;
  function elapsed() {
    if (!current || !['running', 'queued'].includes(current.status)) {$('elapsed-time').textContent = ''; return;}
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - current.created_at));
    const value = seconds >= 60 ? Math.floor(seconds / 60) + ' мин ' + seconds % 60 + ' с' : seconds + ' с';
    $('elapsed-time').textContent = 'С начала запуска · ' + value;
  }
  function draw() {
    const view = derive(current, events);
    if (lastActive !== view.active) inspected = null;
    lastActive = view.active;
    const selected = inspected || view.active;
    for (const [index, button] of stageButtons.entries()) {
      const id = button.dataset.stage; button.dataset.state = view.states[id];
      button.classList.toggle('current', view.running && id === view.active);
      button.classList.toggle('selected', id === selected);
      button.setAttribute('aria-pressed', String(id === selected));
      if (view.running && id === view.active) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
      button.querySelector('i').textContent = view.states[id] === 'done' ? '✓' : ['attention', 'error'].includes(view.states[id]) ? '!' : String(index + 1).padStart(2, '0');
      button.querySelector('small').textContent = words[view.states[id]];
    }
    const active = agentById[view.active];
    const headline = {idle: 'Шесть агентов. Одна задача.', queued: 'Исследование в очереди',
      completed: 'Материалы исследования готовы', needs_clarification: 'Нужно ваше уточнение',
      failed: 'Выполнение прервано', cancelled: 'Исследование остановлено'};
    $('active-agent').textContent = view.running ? active.name : headline[view.status];
    $('agent-status-label').textContent = view.running ? 'СЕЙЧАС РАБОТАЕТ' : view.status === 'idle' ? 'ВАША КОМАНДА АГЕНТОВ' : view.status === 'completed' && current?.result?.search?.status === 'partial' ? 'ПРЕДВАРИТЕЛЬНЫЙ РЕЗУЛЬТАТ · ЕСТЬ ОГРАНИЧЕНИЯ' : 'СТАТУС ИССЛЕДОВАНИЯ';
    $('agent-orb').classList.toggle('working', view.running);
    $('agent-orb').dataset.stage = view.active || 'idle';
    $('agent-orb').textContent = view.status === 'completed' ? '✓' : view.status === 'failed' ? '!' : '✦';
    $('stage-progress').setAttribute('aria-valuemax', String(view.total));
    $('stage-progress').setAttribute('aria-valuenow', String(view.done));
    $('stage-progress-fill').style.width = (view.done / view.total * 100) + '%';
    $('completed-stages').textContent = view.status === 'idle' ? 'Процесс появится после запуска' : 'Завершено этапов: ' + view.done + ' из ' + view.total;
    $('agent-description').textContent = selected ? agentById[selected].description : view.status === 'completed' ? 'Откройте результаты ниже. Цитаты и ограничения доступны в каждой карточке.' : 'Нажмите на этап, чтобы узнать, за что отвечает агент.';
    const meaningful = events.filter(event => eventStage(event) && !['budget', 'checkpoint'].includes(event.kind)).slice(-5);
    $('activity-wrap').hidden = !meaningful.length;
    if (view.running) $('activity-wrap').open = true;
    $('activity-feed').replaceChildren();
    for (const event of meaningful.reverse()) {
      const item = el('li'), detail = event.detail || {};
      const time = new Date(event.created_at * 1000).toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
      const text = toolWords[detail.tool] || detail.message || detail.reason || 'Выполняет этап';
      item.append(el('time', time), el('span', text), el('small', agentById[eventStage(event)].label));
      $('activity-feed').append(item);
    }
    elapsed();
  }
  stageButtons.forEach(button => {button.onclick = () => {inspected = button.dataset.stage; draw();};});
  window.updateResearchProgress = (run, runEvents = []) => {
    current = run; events = runEvents; clearInterval(ticker); ticker = null; draw();
    if (current && ['queued', 'running'].includes(current.status)) ticker = setInterval(elapsed, 1000);
  };
  window.updateResearchProgress(null);
})();
