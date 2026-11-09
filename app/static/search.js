"use strict";

// All external text is rendered with textContent; source links are allowlisted.
window.renderSearch = function renderSearch(container, search) {
  // Explain historical demo runs as well, without rewriting saved results.
  const localWithoutActs = search.coverage?.source === "local" && !(search.cases || []).length;
  if (localWithoutActs) {
    search = { ...search, report: {
      summary: "Внешний поиск не выполнялся. Это демонстрационный режим, а в локальной библиотеке нет подходящих судебных актов.",
      next_steps: [
        "Добавьте реальные тексты актов в библиотеку для локального поиска.",
        "Для внешнего поиска сначала нужен действующий доступ к источнику. Переключение на GigaChat само по себе этого не решает."
      ] }};
  }
  const section = el("section", undefined, "search-results");
  section.append(el("h2", "Судебная практика"));
  section.append(el("p", "Базовая версия · " + (search.query || ""), "run-meta"));
  for (const warning of search.warnings || []) {
    section.append(el("p", warning, "notice warn"));
  }
  if (search.report) {
    section.append(el("p", cleanReportText(search.report.summary), "notice"));
  }
  if (localWithoutActs || search.coverage?.remote_failed) {
    const links = el("div", undefined, "source-actions");
    for (const [label, url] of [["Открыть Банк решений", "https://ras.arbitr.ru/"],
      ["Открыть КАД", "https://kad.arbitr.ru/"]]) {
      const link = el("a", label, "secondary");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      links.append(link);
    }
    const library = el("button", "Добавить акт в библиотеку", "secondary");
    library.type = "button";
    library.onclick = () => {
      const panel = document.querySelector(".library-panel details");
      panel.open = true;
      panel.scrollIntoView({ behavior: "smooth" });
      $("act-number").focus();
    };
    links.append(library);
    section.append(links);
  }
  const relevance = {
    high: "Высокая",
    medium: "Средняя",
    low: "Низкая",
    unknown: "Не оценена"
  };
  for (const item of search.cases || []) {
    const card = el("article", undefined, "case-card");
    card.append(el("h3", item.number));
    card.append(el("p", [item.court, item.date, item.kind].filter(Boolean).join(" · "), "run-meta"));
    if (item.provenance === "user_import_unverified") {
      card.append(el("p", "Текст загружен пользователем; соответствие оригиналу не проверено.", "hint"));
    }
    const comparison = (search.comparisons || []).find(value => value.case_id === item.id);
    if (comparison) {
      card.append(el("span", "Применимость: " + relevance[comparison.relevance], "source-tag"));
      card.append(el("p", "Совпадения: " + comparison.similarities));
      card.append(el("p", "Отличия: " + comparison.differences));
    }
    card.append(el("p", "Обжалование: " + item.appeal_status, "hint"));
    if (item.read_error) card.append(el("p", item.read_error, "notice warn"));
    for (const evidence of search.evidence || []) {
      if (evidence.case_id !== item.id) continue;
      const detail = el("details");
      detail.append(el("summary", "Цитата · страница " + evidence.page));
      detail.append(el("blockquote", evidence.quote));
      detail.append(el("p", evidence.observation));
      card.append(detail);
    }
    for (const [label, value] of [
        ["Карточка дела", item.case_url],
        ["Судебный акт", item.file_url]
      ]) {
      try {
        const url = new URL(value);
        if (url.protocol !== "https:" || !["kad.arbitr.ru", "ras.arbitr.ru"].includes(url.hostname)) continue;
        const link = el("a", label, "secondary");
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        card.append(link);
      } catch {
        /* Missing or invalid upstream URL is never turned into a link. */
      }
    }
    section.append(card);
  }
  if (search.critique) {
    section.append(el("h3", "Замечания критика"));
    for (const concern of search.critique.concerns) section.append(el("p", cleanReportText(concern), "notice warn"));
  }
  if (search.report?.next_steps?.length) {
    section.append(el("h3", "Следующие действия"));
    const list = el("ul");
    for (const step of search.report.next_steps) list.append(el("li", cleanReportText(step)));
    section.append(list);
  }
  for (const limit of search.limits || []) section.append(el("p", limit, "hint"));
  const print = el("button", "Печать / сохранить PDF", "secondary");
  print.type = "button";
  print.onclick = () => window.print();
  section.append(print);
  container.append(section);
};

function cleanReportText(value) {
  return value.replace(/\\n/g, "\n").replace(/\\"\s*[,}\]\s]*$/, "").trim();
}

async function refreshLibrary() {
  const items = await api("/api/library");
  $("library-list").replaceChildren();
  $("library-list").append(el("p", "Актов в списке: " + items.length, "hint"));
  for (const item of items) $("library-list").append(el("span", item.number, "source-tag"));
}

$("library-form").onsubmit = async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    await api("/api/library", {
      method: "POST",
      body: JSON.stringify({
        number: $("act-number").value,
        court: $("act-court").value,
        url: $("act-url").value,
        text: $("act-text").value
      }),
    });
    $("library-message").textContent = "Акт сохранён. Он доступен для локального поиска.";
    await refreshLibrary();
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
  }
};

(async function connections() {
  try {
    const settings = await api("/api/config");
    $("connections").append(el("span", "База: " + settings.database, "source-tag"));
    $("connections").append(el("span", settings.gigachat_model + " · " + settings.model_framework, "source-tag"));
    $("connections").append(el("span", "GigaChat: " + (settings.gigachat_ready ? "ключ задан" : "не настроен"), "source-tag"));
    $("connections").append(el("span", "Parser API: " + (settings.parser_configured ? "ключ задан" : "не настроен"), "source-tag"));
    const check = el("button", "Проверить авторизацию GigaChat", "text-button");
    const message = el("span", "Наличие ключа не подтверждает доступ к сервису.", "hint");
    check.onclick = async () => {
      check.disabled = true;
      message.textContent = "Проверяем соединение…";
      try {
        const result = await api("/api/connections/gigachat/check", {
          method: "POST"
        });
        message.textContent = result.message;
      } catch (error) {
        message.textContent = error.message;
      } finally {
        check.disabled = false;
      }
    };
    $("connections").append(check, message);
    const parserCheck = el("button", "Проверить КАД и поиск по текстам", "text-button");
    const parserStatus = el("span", "КАД и Банк решений проверяются отдельно. Проверка делает 2 API-запроса.", "hint");
    parserStatus.setAttribute("role", "status");
    parserCheck.onclick = async () => {
      parserCheck.disabled = true;
      parserStatus.textContent = "Проверяем КАД и Банк решений без кеша…";
      try {
        const result = await api("/api/connections/parser/check", {method: "POST"});
        parserStatus.textContent = [
          "КАД: " + (result.kad.ok ? "доступен" : result.kad.message),
          "Поиск по текстам RAS: " + (result.ras.ok ? "доступен" : result.ras.message)
        ].join(". ");
      } catch (error) { parserStatus.textContent = error.message; }
      finally { parserCheck.disabled = false; }
    };
    $("connections").append(parserCheck, parserStatus);
    const kadForm = el("form");
    const kadLabel = el("label", "Загрузить реальные акты из КАД по номеру дела");
    kadLabel.htmlFor = "kad-number";
    const kadNumber = el("input");
    kadNumber.id = "kad-number";
    kadNumber.placeholder = "А70-7129/2024";
    kadNumber.required = true;
    kadNumber.maxLength = 30;
    const kadSubmit = el("button", "Получить акты из КАД", "secondary");
    kadSubmit.type = "submit";
    const kadMessage = el("p", "До 3 актов из карточки дела. Загрузка использует API. Это получение известного дела, а не поиск аналогов по теме.", "hint");
    kadMessage.setAttribute("role", "status");
    kadForm.append(kadLabel, kadNumber, kadSubmit, kadMessage);
    kadForm.onsubmit = async event => {
      event.preventDefault();
      kadSubmit.disabled = true;
      kadMessage.textContent = "Получаем карточку и тексты PDF…";
      try {
        const result = await api("/api/library/kad?number=" + encodeURIComponent(kadNumber.value.trim()), {method: "POST"});
        kadMessage.textContent = result.message + ". Получено актов: " + result.imported.length + ". " + result.warnings.join(". ");
        await refreshLibrary();
      } catch (error) { kadMessage.textContent = error.message; }
      finally { kadSubmit.disabled = false; }
    };
    $("library-form").before(kadForm);
    await refreshLibrary();
  } catch (error) {
    showError(error);
  }
})();
