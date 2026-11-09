"use strict";

function cleanReportText(value) {
  return String(value || '').replace(/\\n/g, '\n').replace(/\\"\s*[,}\]\s]*$/, '').trim();
}

function sourceLink(label, value, page) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443') || !['kad.arbitr.ru', 'ras.arbitr.ru'].includes(url.hostname)) return null;
    if (page) url.hash = 'page=' + page;
    const link = el('a', label, 'secondary');
    link.href = url.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  } catch { return null; }
}

function citation(identity, evidence, reveal) {
  const item = evidence.find(e => e.id === identity);
  const button = el('button', item ? 'Источник · с. ' + item.page : 'Источник недоступен', 'citation-link');
  button.type = 'button';
  button.disabled = !item;
  button.onclick = () => {
    let detail = document.getElementById('evidence-' + identity);
    if (!detail && reveal) {
      reveal();
      detail = document.getElementById('evidence-' + identity);
    }
    if (!detail) return;
    let parent = detail.parentElement;
    while (parent) {
      if (parent.tagName === 'DETAILS') parent.open = true;
      parent = parent.parentElement;
    }
    detail.open = true;
    detail.scrollIntoView({behavior: 'smooth', block: 'center'});
    detail.querySelector('summary').focus();
  };
  return button;
}

window.renderSearch = function renderSearch(container, search) {
  const acts = search.cases || [];
  const evidence = (search.evidence || []).map((e, i) => ({...e, id: e.id || 'legacy-' + i}));
  const section = el('section', undefined, 'search-results');
  let revealEvidence = () => {};
  section.append(el('h2', 'Судебная практика'));
  const coverage = search.coverage || {};
  const localOnly = search.external_search_attempted === false || coverage.source === 'local';
  section.append(el('p', localOnly ? 'Локальная библиотека · внешний поиск не выполнялся' : 'Поиск по доступным источникам · охват ограничен', 'run-meta'));
  for (const warning of search.warnings || []) section.append(el('p', warning, 'notice warn'));
  if (search.report) section.append(el('p', cleanReportText(search.report.summary), 'notice'));
  const allQueries = [...(coverage.queries || []), ...(coverage.counterexample_search?.queries || [])];
  if (allQueries.length) {
    const log = el('details', undefined, 'query-log');
    log.append(el('summary', 'Как искали · запросов: ' + allQueries.length));
    const list = el('ol');
    for (const q of allQueries) list.append(el('li', q.query + ' — ' + (q.local_count || 0) + ' локальных, ' + (q.remote_count || 0) + ' внешних' + (q.error ? ' · ' + q.error : '')));
    log.append(list, el('p', 'Кандидатов: ' + (coverage.candidate_acts || 0) + ' · Отобрано актов: ' + acts.length + '. Ранжирование не является оценкой вероятности выигрыша.', 'hint'));
    section.append(log);
  }
  if (search.report?.conclusions?.length) {
    const report = el('section', undefined, 'report-conclusions');
    report.append(el('h3', 'Предварительные выводы'));
    for (const conclusion of search.report.conclusions) {
      const item = el('article');
      item.append(el('p', cleanReportText(conclusion.text)));
      for (const identity of conclusion.evidence_ids || []) item.append(citation(identity, evidence, () => revealEvidence()));
      report.append(item);
    }
    report.append(el('p', 'Ссылки проверены. Смысловую обоснованность выводов проверяет юрист.', 'hint'));
    section.append(report);
  }
  if (!acts.length || coverage.remote_failed) {
    const actions = el('div', undefined, 'source-actions');
    actions.append(sourceLink('Банк решений ↗', 'https://ras.arbitr.ru/'), sourceLink('КАД ↗', 'https://kad.arbitr.ru/'));
    const add = el('button', 'Добавить акт в библиотеку', 'secondary');
    add.type = 'button'; add.onclick = () => $('nav-library').click(); actions.append(add);
    section.append(actions);
  }
  const relevance = {high: 'Высокая', medium: 'Средняя', low: 'Низкая', unknown: 'Не оценена'};
  const perspective = {favorable: 'В пользу позиции клиента', unfavorable: 'Против позиции клиента', mixed: 'Смешанная практика', unknown: 'Сторона не определена'};
  const groups = new Map();
  for (const act of acts) {
    const key = act.case_key || act.number;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(act);
  }
  const toolbar = el('div', undefined, 'results-toolbar');
  const filter = el('input'); filter.type = 'search'; filter.placeholder = 'Найти в подборке'; filter.setAttribute('aria-label', 'Фильтр судебной практики');
  const order = el('select'); order.setAttribute('aria-label', 'Порядок судебных дел');
  for (const [value, label] of [['relevance', 'По применимости'], ['number', 'По номеру дела']]) {
    const option = el('option', label); option.value = value; order.append(option);
  }
  toolbar.append(filter, order);
  const cards = el('div');
  if (groups.size) section.append(toolbar, cards);
  function drawCards() {
    cards.replaceChildren();
    let count = 0;
    const grouped = [...groups.values()].sort((a, b) => {
      if (order.value === 'number') return a[0].number.localeCompare(b[0].number, 'ru');
      const score = group => Math.max(...group.map(act => ({high: 3, medium: 2, low: 1, unknown: 0}[(search.comparisons || []).find(c => c.case_id === act.id)?.relevance || 'unknown'])));
      return score(b) - score(a);
    });
    for (const group of grouped) {
      if (!group.some(a => [a.number, a.court, a.kind].join(' ').toLowerCase().includes(filter.value.toLowerCase()))) continue;
      count++;
      const card = el('article', undefined, 'case-card');
      const head = el('div', undefined, 'case-top');
      head.append(el('h3', group[0].number), el('span', 'АКТОВ: ' + group.length, 'source-tag'));
      card.append(head);
      for (const act of group) {
        const body = el('div', undefined, group.length > 1 ? 'case-act' : '');
        body.append(el('p', [act.court, act.date && (act.date_kind === 'source_registration_or_publication' ? act.date + ' (дата источника)' : act.date), act.kind].filter(Boolean).join(' · '), 'run-meta'));
        if (act.provenance === 'user_import_unverified') body.append(el('p', 'Загружено пользователем · соответствие оригиналу не проверено', 'notice warn'));
        const comparison = (search.comparisons || []).find(c => c.case_id === act.id);
        if (comparison) {
          body.append(el('span', 'Применимость: ' + (relevance[comparison.relevance] || relevance.unknown), 'source-tag relevance-' + comparison.relevance));
          body.append(el('p', perspective[comparison.perspective || 'unknown'], 'perspective'));
          const columns = el('div', undefined, 'comparison-columns');
          for (const [title, text] of [['СОВПАДЕНИЯ', comparison.similarities], ['СУЩЕСТВЕННЫЕ ОТЛИЧИЯ', comparison.differences]]) {
            const column = el('div'); column.append(el('h4', title), el('p', text)); columns.append(column);
          }
          body.append(columns);
          for (const finding of comparison.findings || []) {
            const detail = el('div', undefined, 'finding'); detail.append(el('p', finding.text));
            for (const identity of finding.evidence_ids || []) detail.append(citation(identity, evidence, () => revealEvidence()));
            body.append(detail);
          }
        }
        body.append(el('p', 'История обжалования: ' + (act.appeal_status || 'Не проверена'), 'hint'));
        if (act.read_error) body.append(el('p', act.read_error, 'notice warn'));
        if (act.extraction_quality?.ocr_used) body.append(el('p', 'Использован OCR · распознанный текст требует сверки с оригиналом', 'notice warn'));
        if (act.extraction_quality?.empty_pages?.length) body.append(el('p', 'Страницы без доступного текста: ' + act.extraction_quality.empty_pages.join(', '), 'notice warn'));
        if (act.history && act === group[0]) {
          const caseHistory = coverage.case_histories?.[act.number] || act.history;
          const history = el('details');
          history.append(el('summary', 'История дела · опубликованных актов: ' + (caseHistory.act_ids?.length || group.length)));
          history.append(el('p', caseHistory.history_complete ? 'Снимок доступных источников получен. Юридическая окончательность не подтверждена.' : 'Полнота опубликованной цепочки не подтверждена.', 'hint'));
          for (const relation of caseHistory.links || []) history.append(el('p', relation.effect || 'Связанные акты последовательных инстанций'));
          body.append(history);
        }
        for (const item of evidence.filter(e => (e.act_id || e.case_id) === act.id)) {
          const detail = el('details', undefined, 'evidence-item'); detail.id = 'evidence-' + item.id;
          const summary = el('summary', 'Цитата · страница ' + item.page); summary.tabIndex = 0;
          detail.append(summary, el('blockquote', item.quote), el('p', item.observation));
          const categories = {claims: 'требования', objections: 'возражения', evidence: 'доказательства', facts: 'обстоятельства', reasoning: 'мотивы', outcome: 'результат', unknown: 'раздел не определён'};
          const assertions = {party_argument: 'довод стороны', court_finding: 'возможный вывод суда', unknown: 'тип не определён'};
          detail.append(el('p', 'Предварительная разметка: ' + (categories[item.category] || categories.unknown) + ' · ' + (assertions[item.assertion_type] || assertions.unknown) + '. Требуется сверка.', 'hint'));
          if ((act.page_quality || []).some(q => q.page === item.page && q.origin === 'ocr')) detail.append(el('p', 'Текст этой страницы получен через OCR.', 'notice warn'));
          const original = sourceLink('Открыть страницу оригинала ↗', act.file_url, item.page);
          if (original) detail.append(original);
          if (act.original_retained && /^[a-f0-9]{64}$/.test(act.document_sha256 || '')) {
            const saved = el('a', 'Сохранённый оригинал · страница ' + item.page, 'secondary');
            saved.href = '/api/documents/' + act.document_sha256 + '/original#page=' + item.page;
            saved.target = '_blank'; saved.rel = 'noopener noreferrer'; detail.append(saved);
          }
          body.append(detail);
        }
        for (const [label, url] of [['Карточка дела ↗', act.case_url], ['Судебный акт ↗', act.file_url]]) {
          const link = sourceLink(label, url); if (link) body.append(link);
        }
        const retrieval = act.retrieval;
        if (retrieval) body.append(el('p', 'Найдено по запросам: ' + [...new Set(retrieval.origins.map(o => o.query))].join('; '), 'hint'));
        card.append(body);
      }
      cards.append(card);
    }
    if (!count) cards.append(el('p', 'В этой подборке нет дел по такому фильтру.', 'notice'));
  }
  filter.oninput = drawCards; order.onchange = drawCards; drawCards();
  revealEvidence = () => {filter.value = ''; drawCards();};
  if (search.critique) {
    section.append(el('h3', 'Проверка и ограничения'));
    for (const concern of search.critique.concerns || []) section.append(el('p', cleanReportText(concern), 'notice warn'));
  }
  for (const [title, values] of [['Что ещё нужно подтвердить', search.report?.missing_evidence], ['Следующие действия', search.report?.next_steps]]) {
    if (!values?.length) continue;
    section.append(el('h3', title)); const list = el('ul');
    for (const value of values) list.append(el('li', cleanReportText(value))); section.append(list);
  }
  for (const limit of search.limits || []) section.append(el('p', limit, 'hint'));
  const print = el('button', 'Печать / сохранить PDF', 'secondary'); print.type = 'button'; print.onclick = () => window.print();
  section.append(print); container.append(section);
};

async function refreshLibrary() {
  const items = await api('/api/library'); $('library-list').replaceChildren();
  $('library-list').append(el('p', 'Актов в списке: ' + items.length, 'hint'));
  for (const item of items) $('library-list').append(el('span', item.number, 'source-tag'));
}

$('library-form').onsubmit = async event => {
  event.preventDefault(); const button = event.currentTarget.querySelector('button'); button.disabled = true;
  try {
    await api('/api/library', {method: 'POST', body: JSON.stringify({number: $('act-number').value, court: $('act-court').value, url: $('act-url').value, text: $('act-text').value})});
    $('library-message').textContent = 'Акт сохранён. Он доступен для локального поиска.'; await refreshLibrary();
  } catch (error) { showError(error); } finally { button.disabled = false; }
};

(async function connections() {
  try {
    const settings = await api('/api/config');
    const root = $('connections');
    function card(title, state, description, action, enabled, run) {
      const box = el('section', undefined, 'connection-card');
      const badge = el('span', state, 'connection-state');
      badge.dataset.state = enabled ? 'unchecked' : 'missing';
      const button = el('button', action, 'secondary'); button.type = 'button';
      button.disabled = !enabled;
      const message = el('p', enabled ? 'Доступ ещё не проверен.' : 'Добавьте ключ в локальный .env и перезапустите приложение.', 'connection-message');
      message.setAttribute('role', 'status');
      button.onclick = async () => {
        button.disabled = true; message.textContent = 'Проверяем соединение…';
        try {
          const result = await run(); message.textContent = result.message;
          badge.textContent = result.ok ? 'Проверено: доступен' : 'Проверка не пройдена';
          badge.dataset.state = result.ok ? 'ready' : 'missing';
        } catch (error) {message.textContent = error.message; badge.textContent = 'Не удалось проверить'; badge.dataset.state = 'missing';}
        finally {button.disabled = !enabled;}
      };
      box.append(badge, el('h3', title), el('p', description), button, message); root.append(box);
      return {badge, message, button};
    }
    card('GigaChat', settings.gigachat_ready ? 'Ключ задан' : 'Нужен ключ',
      'Разбор ситуации, исследование, сравнение и смысловая проверка. Тексты отправляются после вашего согласия.',
      'Проверить авторизацию', settings.gigachat_ready,
      () => api('/api/connections/gigachat/check', {method: 'POST'}));
    card('КАД и Банк решений', settings.parser_configured ? 'Ключ задан' : 'Нужен Parser API',
      'Карточки дел, опубликованные акты и PDF поступают через Parser API. Поиск по текстам — в Банке решений.',
      'Проверить источники', settings.parser_configured, async () => {
        const r = await api('/api/connections/parser/check', {method: 'POST'});
        return {ok: r.kad.ok && r.ras.ok, message: ['КАД: ' + (r.kad.ok ? 'доступен' : r.kad.message), 'Банк решений: ' + (r.ras.ok ? 'доступен' : r.ras.message)].join('. ')};
      });
    const ocr = card('Оригиналы PDF и OCR', 'Проверяем на компьютере',
      'Оригиналы сохраняются в локальном архиве. Сканы распознаются на русском и английском; текст можно сверить по странице PDF.',
      'Проверить распознавание', true, () => api('/api/connections/ocr'));
    ocr.message.textContent = 'Проверяем доступность движка…';
    try {
      const r = await api('/api/connections/ocr');
      ocr.badge.textContent = !r.enabled ? 'OCR выключен' : r.ok ? 'OCR доступен' : 'Нужен Tesseract';
      ocr.badge.dataset.state = r.ok && r.enabled ? 'ready' : 'missing';
      ocr.message.textContent = r.message;
    } catch (error) {ocr.badge.textContent = 'Не удалось проверить OCR'; ocr.message.textContent = error.message;}
    const features = el('div', undefined, 'research-settings');
    for (const label of [
      'Хранилище: ' + settings.database,
      'Исследователь: ' + (settings.tool_research_enabled ? 'включён в GigaChat' : 'базовый режим'),
      'Смысловой критик: ' + (settings.semantic_critic_enabled ? 'включён в GigaChat' : 'выключен'),
      'Экспертная разметка: ожидается'
    ]) features.append(el('span', label, 'source-tag'));
    root.append(features, el('p', 'Проверка КАД и Банка решений использует два обращения к Parser API. Наличие ключа не подтверждает доступ, а история опубликованных актов не гарантирует юридическую окончательность.', 'connection-note'));
    const form = el('form', undefined, 'import-kad');
    const label = el('label', 'Загрузить акты по номеру дела'); label.htmlFor = 'kad-number';
    const input = el('input'); input.id = 'kad-number'; input.placeholder = 'А70-7129/2024'; input.required = true; input.maxLength = 30;
    const button = el('button', 'Получить акты из КАД', 'secondary'); button.type = 'submit'; button.disabled = !settings.parser_configured;
    const note = el('p', settings.parser_configured ? 'Карточка и PDF загружаются через Parser API; оригиналы сохраняются. Получение известного дела не заменяет поиск аналогов.' : 'Для загрузки нужен ключ Parser API. Текст акта можно добавить вручную в форме ниже.', 'hint'); note.setAttribute('role', 'status');
    form.append(label, input, button, note);
    form.onsubmit = async event => {
      event.preventDefault(); button.disabled = true; note.textContent = 'Получаем карточку и PDF…';
      try {
        const number = input.value.trim().toUpperCase().replace(/^A/, 'А');
        const r = await api('/api/library/kad?number=' + encodeURIComponent(number), {method: 'POST'});
        note.textContent = r.message + '. Актов: ' + r.imported.length + '. ' + r.warnings.join('. '); await refreshLibrary();
      } catch (error) {note.textContent = error.message;}
      finally {button.disabled = !settings.parser_configured;}
    };
    $('library-form').before(form); await refreshLibrary();
  } catch (error) { showError(error); }
})();
