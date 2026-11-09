"use strict";
(function (root) {
  const agents = [
    {id: 'intake', label: 'Разбор', name: 'Аналитик ситуации', description: 'Выделяет обстоятельства, проверяет источники сведений и готовит уточняющие вопросы.'},
    {id: 'search', label: 'Поиск', name: 'Планировщик поиска', description: 'Подбирает запросы, объединяет результаты и отбирает материалы для исследования.'},
    {id: 'research', label: 'Исследование', name: 'Исследователь дела', description: 'Читает судебные акты и собирает цитаты, на которые можно сослаться.'},
    {id: 'comparison', label: 'Сравнение', name: 'Сопоставитель', description: 'Находит существенные совпадения и отличия от ситуации клиента.'},
    {id: 'critique', label: 'Критика', name: 'Агент-критик', description: 'Проверяет основания выводов и отмечает вопросы, которые ещё требуют исследования.'},
    {id: 'report', label: 'Отчёт', name: 'Редактор отчёта', description: 'Собирает предварительную записку со ссылками на доказательства и ограничениями.'}
  ];
  const ids = agents.map(a => a.id);
  function eventStage(event) {
    const detail = event.detail || {};
    if (ids.includes(detail.stage)) return detail.stage;
    if (event.kind === 'tool' && detail.tool) return 'intake';
    return null;
  }
  function derive(run, events = []) {
    const planned = run?.payload?.search_enabled ? ids : ['intake'];
    const states = Object.fromEntries(ids.map(id => [id, planned.includes(id) ? 'waiting' : 'skipped']));
    const visited = new Set();
    let active = 'intake';
    for (const event of events) {
      const stage = eventStage(event);
      if (!stage || !planned.includes(stage)) continue;
      if (visited.size && stage !== active) states[active] = 'done';
      visited.add(stage); active = stage;
      states[stage] = event.kind === 'stage_done' ? 'done' : 'running';
      if (event.kind === 'tool' && event.detail.tool === 'finish' && event.detail.ok) states.intake = 'done';
    }
    const status = run?.status || 'idle';
    if (status === 'queued' || status === 'idle') {
      active = null;
      for (const id of ids) if (states[id] === 'running') states[id] = 'waiting';
      if (status === 'idle') ids.forEach(id => {states[id] = 'waiting';});
    } else if (status === 'needs_clarification') {
      active = 'intake'; states.intake = 'attention';
      for (const id of ids.slice(1)) states[id] = planned.includes(id) ? 'waiting' : 'skipped';
    } else if (status === 'cancelled') {
      states[active] = 'stopped';
    } else if (status === 'failed') {
      states[active] = 'error';
    } else if (status === 'completed') {
      if (run.result) states.intake = 'done';
      for (const id of visited) states[id] = 'done';
      const search = run.result?.search;
      if (search && (search.coverage?.remote_failed || (search.warnings || []).length)) {
        const affected = ids.includes(active) ? active : 'search';
        states[affected] = 'attention';
      }
      for (const id of planned) if (!visited.has(id) && id !== 'intake') states[id] = 'skipped';
      active = null;
    } else if (status === 'running') states[active] = 'running';
    const done = planned.filter(id => states[id] === 'done' ||
      (status === 'completed' && states[id] === 'attention')).length;
    return {states, active, done, total: status === 'idle' ? 6 : planned.length,
      status, running: status === 'running', visited: [...visited]};
  }
  const api = {agents, derive, eventStage};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.DovodProgress = api;
})(typeof window === 'undefined' ? {} : window);
