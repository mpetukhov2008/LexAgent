"use strict";
const $ = id => document.getElementById(id);
const labels = {
  platform: "Площадка",
  object: "Объект",
  rights: "Основание прав",
  placement: "Способ размещения",
  demands: "Требования",
  event_date: "Дата события"
};
const statuses = {
  queued: "В ОЧЕРЕДИ",
  running: "В РАБОТЕ",
  completed: "РАЗБОР ЗАВЕРШЁН",
  needs_clarification: "НУЖНЫ УТОЧНЕНИЯ",
  failed: "ОШИБКА"
};
let config = {},
  currentId = null,
  timer = null,
  lastRendered = "",
  submission = null;

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

function showError(error) {
  $('error').textContent = error.message || String(error);
  $('error').hidden = false;
}

function clearError() {
  $('error').hidden = true;
}
async function api(path, options = {}) {
  const r = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {})
    }
  });
  const value = await r.json();
  if (!r.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'Проверьте поля: ' + JSON.stringify(value.detail));
  return value;
}

function modeNote() {
  const online = $('mode').value !== 'demo';
  $('consent-wrap').hidden = !online;
  $('mode-note').textContent = $('mode').value === 'gigachat' ? (config.gigachat_ready ? 'GigaChat настроен. Доступ проверяется при запросе; подключение не гарантируется наличием ключа.' : 'Укажите GIGACHAT_AUTH_KEY в .env.') : online ? (config.yandex_ready ? 'Тексты обрабатывает Alice AI через Yandex AI Studio.' : 'API ещё не настроен. Укажите ключ и каталог в .env, затем перезапустите сервер.') : 'Локальный сценарий по правилам. Языковая модель и поиск КАД не вызываются.';
}

function addDocument(name = '', text = '') {
  if ($('documents').children.length >= 6) throw new Error('Можно добавить не более 6 текстов');
  const box = el('div', undefined, 'document'),
    remove = el('button', 'Удалить', 'remove');
  remove.type = 'button';
  remove.onclick = () => box.remove();
  const nameInput = el('input');
  nameInput.value = name;
  nameInput.placeholder = 'Название документа';
  nameInput.maxLength = 120;
  nameInput.required = true;
  nameInput.setAttribute('aria-label', 'Название документа');
  const body = el('textarea');
  body.value = text;
  body.rows = 4;
  body.maxLength = 12000;
  body.required = true;
  body.placeholder = 'Вставьте текст документа';
  body.setAttribute('aria-label', 'Текст документа');
  box.append(remove, nameInput, body);
  $('documents').append(box);
}

function loadSample(sample) {
  $('title').value = sample.title;
  $('narrative').value = sample.narrative;
  $('role').value = sample.role;
  $('mode').value = 'demo';
  $('consent').checked = false;
  $('documents').replaceChildren();
  sample.documents.forEach(d => addDocument(d.name, d.text));
  modeNote();
}
async function history() {
  const runs = await api('/api/runs');
  $('history').replaceChildren();
  for (const run of runs) {
    const b = el('button', run.title);
    b.append(el('small', statuses[run.status] + ' · ' + run.mode.toUpperCase()));
    b.onclick = () => selectRun(run.id);
    $('history').append(b);
  }
}

function render(run) {
  $('empty').hidden = true;
  $('result').hidden = false;
  $('run-actions').hidden = false;
  $('trace-wrap').hidden = false;
  $('run-status').textContent = run.result?.search?.status === 'partial' ? 'ЧАСТИЧНЫЙ РЕЗУЛЬТАТ' : statuses[run.status];
  if (run.result?.search?.coverage?.source === 'local' && !run.result.search.cases?.length) {
    $('run-status').textContent = 'НЕТ ДАННЫХ ДЛЯ ПОИСКА';
  }
  $('export').href = '/api/runs/' + run.id + '/export';
  $('retry').hidden = run.status !== 'failed';
  const box = $('result');
  box.replaceChildren();
  box.append(el('div', run.payload.title, 'run-meta'));
  box.append(el('div', (run.payload.mode === 'demo' ? 'ДЕМОНСТРАЦИЯ БЕЗ МОДЕЛИ' : run.payload.mode.toUpperCase()) + ' · Попытка ' + run.attempts, 'run-meta'));
  if (run.parent_id) box.append(el('div', 'Новая версия после уточнений. Исходный запуск сохранён.', 'run-meta'));
  if (run.error) box.append(el('p', run.error, 'notice fail'));
  if (['queued', 'running'].includes(run.status)) {
    const p = el('p', undefined, 'notice');
    p.append(el('span', undefined, 'spinner'), document.createTextNode('Агент изучает источники. Можно оставить страницу открытой.'));
    box.append(p);
    return;
  }
  if (!run.result) return;
  const result = run.result;
  const metrics = el('div', undefined, 'result-metrics');
  for (const [value, label] of [
      [result.facts.length, 'сведений с источниками'],
      [result.questions.length, 'уточнений'],
      [result.search?.cases?.length || 0, 'дел в подборке']
    ]) {
    const metric = el('div');
    metric.append(el('strong', String(value)), el('span', label));
    metrics.append(metric);
  }
  box.append(metrics);
  const reuse = el('button', 'Взять за основу нового разбора', 'text-button');
  reuse.type = 'button';
  reuse.onclick = () => {
    loadSample(run.payload);
    $('mode').value = run.payload.mode;
    $('search-enabled').checked = Boolean(run.payload.search_enabled);
    $('search-query').value = run.payload.search_query || '';
    modeNote();
    $('title').focus();
    $('intake-form').scrollIntoView({
      behavior: 'smooth',
      block: 'start'
    });
  };
  box.append(reuse);
  box.append(el('p', result.ready_for_search ? 'Исходная карточка заполнена. Результаты поиска, если он включён, показаны ниже.' : 'Нужны уточнения. Система сохранила пробелы и не подставляла догадки.', 'notice' + (result.questions.length ? ' warn' : '')));
  for (const conflict of result.conflicts) box.append(el('p', conflict.message, 'notice warn'));
  for (const [key, label] of Object.entries(labels)) {
    const section = el('div', undefined, 'field');
    section.append(el('h3', label.toUpperCase()));
    const facts = result.fields[key];
    if (!facts.length) section.append(el('div', 'Не установлено по представленным сведениям', 'unknown'));
    for (const fact of facts) {
      const f = el('div', undefined, 'fact');
      f.append(el('div', fact.value));
      f.append(el('span', fact.status === 'client_statement' ? 'Со слов клиента' : 'Указано в документе', 'source-tag'));
      const source = el('details', undefined, 'evidence-detail');
      source.append(el('summary', fact.source_name + ' · показать цитату'), el('blockquote', fact.quote));
      f.append(source);
      section.append(f);
    }
    box.append(section);
  }
  if (result.questions.length) {
    const form = el('form', undefined, 'questions');
    form.append(el('h3', 'Уточнения для юриста'));
    for (const q of result.questions) {
      const label = el('label', q.question);
      const input = el('textarea');
      input.name = q.field;
      input.maxLength = 1000;
      input.rows = 2;
      label.append(input);
      form.append(label);
    }
    const submit = el('button', 'Сохранить уточнения и повторить разбор', 'primary');
    let followupConsent = null;
    if (run.payload.mode !== 'demo') {
      const consentLabel = el('label', undefined, 'checkbox');
      followupConsent = el('input');
      followupConsent.type = 'checkbox';
      followupConsent.required = true;
      consentLabel.append(followupConsent, document.createTextNode('Разрешаю передать уточнения и исходные тексты в ' + run.payload.mode.toUpperCase()));
      form.append(consentLabel);
    }
    submit.type = 'submit';
    form.append(submit);
    form.onsubmit = async event => {
      event.preventDefault();
      clearError();
      const answers = {};
      for (const input of form.querySelectorAll('textarea'))
        if (input.value.trim()) answers[input.name] = input.value.trim();
      if (!Object.keys(answers).length) return showError(new Error('Введите хотя бы одно уточнение'));
      const cloudConsent = Boolean(followupConsent?.checked);
      if (followupConsent && !cloudConsent) return showError(new Error('Подтвердите передачу уточнений выбранной модели'));
      submit.disabled = true;
      try {
        const child = await api('/api/runs/' + run.id + '/clarifications', {
          method: 'POST',
          headers: {
            'Idempotency-Key': crypto.randomUUID()
          },
          body: JSON.stringify({
            answers,
            consent_to_yandex: cloudConsent,
            consent_to_cloud: cloudConsent
          })
        });
        await selectRun(child.id);
        await history();
      } catch (e) {
        showError(e);
      } finally {
        submit.disabled = false;
      }
    };
    box.append(form);
  }
  if (result.search && window.renderSearch) window.renderSearch(box, result.search);
  if (result.search_notice) box.append(el('p', result.search_notice, 'notice warn'));
  box.append(el('p', result.limits[1], 'hint'));
}
async function refreshRun() {
  const id = currentId;
  if (!id) return;
  const [run, events] = await Promise.all([api('/api/runs/' + id), api('/api/runs/' + id + '/events')]);
  if (currentId !== id) return;
  const signature = id + run.status + run.updated_at;
  if (signature !== lastRendered) {
    render(run);
    lastRendered = signature;
  }
  const stageNames = ['intake', 'search', 'research', 'comparison', 'critique', 'report'];
  const activeStage = [...events].reverse().find(event => stageNames.includes(event.detail.stage))?.detail.stage || 'intake';
  document.querySelectorAll('.pipeline span').forEach((node, index) => node.classList.toggle('current', stageNames[index] === activeStage));
  $('trace').replaceChildren();
  for (const event of events) {
    const li = el('li');
    li.append(el('b', (event.detail.stage ? event.detail.stage + ' · ' : '') + (event.detail.tool || event.kind)), el('span', event.detail.reason || event.detail.message || ''));
    if (event.detail.ok === false) li.append(el('span', ' · ' + event.detail.message));
    $('trace').append(li);
  }
  if (['queued', 'running'].includes(run.status)) timer = setTimeout(() => refreshRun().catch(showError), 700);
  else await history();
}
async function selectRun(id) {
  clearTimeout(timer);
  currentId = id;
  lastRendered = '';
  location.hash = id;
  try {
    await refreshRun();
  } catch (e) {
    showError(e);
  }
}
$('intake-form').onsubmit = async event => {
  event.preventDefault();
  clearError();
  $('submit').disabled = true;
  const payload = {
    title: $('title').value,
    narrative: $('narrative').value,
    role: $('role').value,
    mode: $('mode').value,
    consent_to_yandex: $('consent').checked,
    consent_to_cloud: $('consent').checked,
    search_enabled: $('search-enabled').checked,
    search_query: $('search-query').value,
    answers: {},
    documents: [...$('documents').children].map(d => ({
      name: d.querySelector('input').value,
      text: d.querySelector('textarea').value
    }))
  };
  const body = JSON.stringify(payload);
  if (!submission || submission.body !== body) submission = {
    body,
    key: crypto.randomUUID()
  };
  try {
    const run = await api('/api/runs', {
      method: 'POST',
      headers: {
        'Idempotency-Key': submission.key
      },
      body
    });
    submission = null;
    await selectRun(run.id);
    await history();
  } catch (e) {
    showError(e);
  } finally {
    $('submit').disabled = false;
  }
};
$('new-run').onclick = () => {
  clearTimeout(timer);
  currentId = null;
  location.hash = '';
  $('intake-form').reset();
  $('documents').replaceChildren();
  $('empty').hidden = false;
  $('result').hidden = true;
  $('trace-wrap').hidden = true;
  $('run-actions').hidden = true;
  $('run-status').textContent = 'ОЖИДАЕТ ВВОДА';
  clearError();
  modeNote();
};
$('retry').onclick = async () => {
  try {
    await api('/api/runs/' + currentId + '/retry', {
      method: 'POST'
    });
    await selectRun(currentId);
  } catch (e) {
    showError(e);
  }
};
$('mode').onchange = modeNote;
$('add-document').onclick = () => {
  try {
    addDocument();
  } catch (e) {
    showError(e);
  }
};
$('files').onchange = async event => {
  clearError();
  try {
    for (const file of event.target.files) {
      if (!file.name.toLowerCase().endsWith('.txt')) throw new Error('Поддерживаются только TXT');
      if (file.size > 48000) throw new Error('Файл слишком большой');
      const text = new TextDecoder('utf-8', {
        fatal: true
      }).decode(await file.arrayBuffer());
      if (text.length > 12000) throw new Error('В документе больше 12 000 символов');
      addDocument(file.name, text);
    }
  } catch (e) {
    showError(e);
  } finally {
    event.target.value = '';
  }
};
for (const button of document.querySelectorAll('[data-sample]')) button.onclick = async () => {
  clearError();
  try {
    loadSample(await api('/api/samples/' + button.dataset.sample));
  } catch (e) {
    showError(e);
  }
};
(async () => {
  try {
    config = await api('/api/config');
    modeNote();
    await history();
    if (location.hash) await selectRun(location.hash.slice(1));
  } catch (e) {
    showError(e);
  }
})();
