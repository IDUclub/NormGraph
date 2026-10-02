"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const API = "/admin/ui/api";
const STATES = {
  complete: "Завершено",
  incomplete: "Не завершено",
  unknown: "Статус неизвестен",
  no_clauses: "Нет пунктов",
};
const PLANS = {
  auto: "Автопроверка",
  reviewed: "Проверен экспертом",
  unsupported: "Без автопроверки",
  none: "Плана нет",
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function message(text, error = false) {
  const notice = $("#notice");
  notice.textContent = text;
  notice.classList.toggle("error", error);
  notice.classList.toggle("hidden", !text);
}

async function request(url, {method = "GET", body} = {}) {
  let response;
  try {
    response = await fetch(url, {
      method, credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-NormGraph-Admin": "1"},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
  } catch (_) {
    throw new Error("Нет связи с сервером. Если обработка была запущена, проверьте статус документа перед повтором.");
  }
  if ((response.status === 401 || response.status === 403) && url.startsWith(API)) {
    window.location.replace("/admin/ui/login");
    throw new Error("Сессия закончилась или права доступа изменились. Войдите снова.");
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail :
      response.status === 422 ? "Проверьте введённые поля запроса." :
      `Ошибка сервера (${response.status}). Подробности в логах.`);
  }
  return data;
}

function bindLogin() {
  const form = $("#login-form");
  if (!form) return false;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button");
    const data = new FormData(form);
    button.disabled = true;
    $("#login-error").textContent = "";
    try {
      await request("/admin/ui/session", {method: "POST", body: {
        username: data.get("username"), password: data.get("password"),
      }});
      window.location.replace("/admin/ui");
    } catch (error) {
      $("#login-error").textContent = error.message;
    } finally {
      form.elements.password.value = "";
      button.disabled = false;
    }
  });
  return true;
}

let documentsAfter = "";
let documentsRequest = 0;
let documentFilter = {query: "", state: ""};
let currentDocument = null;
let detailRequest = 0;
let itemsRequest = 0;
let collection = "restrictions";
let itemsAfter = "";
let operationRunning = false;
let bulkRunning = false;
let bulkStarting = false;
let bulkStatusKnown = false;
let bulkTimer = null;
let bulkRequest = 0;
let restrictionsAfter = "";
let restrictionsRequest = 0;
let restrictionFilter = {};
let restrictionRequest = 0;
let currentRestriction = null;

function stateTag(state) {
  return node("span", STATES[state] || STATES.unknown, `tag ${state}`);
}

function metadata(element, values) {
  element.replaceChildren();
  for (const [label, value] of values) {
    element.append(node("dt", label), node("dd", value ?? "—"));
  }
}

async function loadOverview() {
  try {
    const data = await request(`${API}/overview`);
    for (const key of ["documents", "clauses", "restrictions", "references"]) {
      $(`#stat-${key}`).textContent = Number(data.stats[key] || 0).toLocaleString("ru-RU");
    }
    $("#sync-status").textContent = `Kafka: ${data.kafka_enabled ? "включена" : "выключена"}. Сверка при запуске: ${data.reconcile_on_startup ? "включена" : "выключена"}.`;
  } catch (error) {
    for (const key of ["documents", "clauses", "restrictions", "references"]) $(`#stat-${key}`).textContent = "—";
    $("#sync-status").textContent = "Состояние недоступно";
    message(error.message, true);
  }
}

async function loadDocuments(append = false) {
  const sequence = ++documentsRequest;
  const more = $("#documents-more");
  more.disabled = true;
  if (!append) {
    documentFilter = {query: $("#doc-query").value.trim(), state: $("#doc-state").value};
    documentsAfter = "";
    $("#documents-body").replaceChildren();
    $("#documents-empty").textContent = "Загрузка документов…";
    $("#documents-empty").classList.remove("hidden");
    more.classList.add("hidden");
  }
  try {
    const params = new URLSearchParams({...documentFilter, after: documentsAfter, limit: "50"});
    const data = await request(`${API}/documents?${params}`);
    if (sequence !== documentsRequest) return;
    for (const doc of data.items) {
      const row = node("tr");
      const name = node("td", doc.name || "Документ-ссылка");
      name.append(node("small", doc.doc_id));
      const version = node("td", doc.version || "—");
      version.append(node("small", doc.corpus || "—"));
      const status = node("td"); status.append(stateTag(doc.state));
      const action = node("td");
      const button = node("button", "Открыть", "button");
      button.addEventListener("click", () => openDocument(doc.doc_id));
      action.append(button);
      row.append(name, version, node("td", doc.clauses), node("td", doc.restrictions), status, action);
      $("#documents-body").append(row);
    }
    const empty = !$("#documents-body").children.length;
    $("#documents-empty").textContent = "Документы не найдены. Проверьте фильтр или загрузите документ из IDU_DVD.";
    $("#documents-empty").classList.toggle("hidden", !empty);
    documentsAfter = data.next_after || "";
    more.classList.toggle("hidden", !data.has_more);
  } catch (error) {
    if (sequence === documentsRequest) {
      message(error.message, true);
      $("#documents-empty").textContent = "Не удалось загрузить документы. Нажмите «Найти / обновить».";
    }
  } finally {
    if (sequence === documentsRequest) more.disabled = false;
  }
}

async function openDocument(docId) {
  const sequence = ++detailRequest;
  ++itemsRequest;
  currentDocument = null;
  $("#detail-name").textContent = "Загрузка…";
  $("#detail-metadata").replaceChildren();
  $("#detail-items").replaceChildren();
  $("#detail-state").textContent = "Читаем состояние документа";
  $("#detail-more").classList.add("hidden");
  for (const id of ["detail-sync", "detail-extract", "detail-refresh"]) $(`#${id}`).disabled = true;
  if (!$("#document-dialog").open) $("#document-dialog").showModal();
  try {
    const doc = await request(`${API}/documents/${encodeURIComponent(docId)}`);
    if (sequence !== detailRequest) return;
    currentDocument = doc;
    $("#detail-name").textContent = doc.name || doc.doc_id;
    const explanations = {
      complete: "Извлечение завершено. Это технический статус, а не оценка полноты найденных норм.",
      incomplete: "Извлечение не завершено: оно может выполняться, быть прерванным или частичным. Обновите статус и проверьте логи.",
      unknown: "Статус извлечения не записан. Оно могло ещё не запускаться либо выполняться старой версией сервиса.",
      no_clauses: "В графе нет пунктов этого документа. Возможно, это только ссылка из другого документа или документ без фрагментов.",
    };
    $("#detail-state").textContent = explanations[doc.state] || explanations.unknown;
    metadata($("#detail-metadata"), [
      ["ID документа", doc.doc_id], ["Версия", doc.version], ["Корпус", doc.corpus],
      ["Пункты", doc.clauses], ["Ограничения", doc.restrictions],
      ["Пользователь / сценарий", doc.user_id ? `${doc.user_id} / ${doc.scenario_id || "—"}` : "Общий корпус"],
      ["Пункты с ошибками", (doc.extraction_failed_clause_ids || []).join("\n") || "Не отмечены"],
    ]);
    $("#detail-refresh").disabled = false;
    $("#detail-sync").disabled = operationRunning || bulkRunning || bulkStarting;
    $("#detail-extract").disabled = operationRunning || bulkRunning || bulkStarting || !doc.clauses;
    await loadItems();
  } catch (error) {
    if (sequence === detailRequest) $("#detail-state").textContent = error.message;
  }
}

async function loadItems(append = false) {
  if (!currentDocument) return;
  const sequence = ++itemsRequest;
  const docId = currentDocument.doc_id;
  const type = collection;
  const more = $("#detail-more");
  more.disabled = true;
  if (!append) {
    itemsAfter = "";
    more.classList.add("hidden");
    $("#detail-items").replaceChildren(node("p", "Загрузка…", "muted"));
  }
  try {
    const params = new URLSearchParams({after: itemsAfter, limit: "50"});
    const data = await request(`${API}/documents/${encodeURIComponent(docId)}/${type}?${params}`);
    if (sequence !== itemsRequest) return;
    if (!append) $("#detail-items").replaceChildren();
    for (const item of data.items) {
      const card = node("article", null, "detail-item");
      if (type === "clauses") {
        card.append(node("h3", item.numbering || "Пункт без номера"), node("p", item.text || "Текст отсутствует"), node("small", item.node_id));
      } else {
        card.append(node("h3", `${item.subject || "—"} → ${item.object || "—"}`));
        card.append(node("p", [item.kind, item.value_operator, item.value_number, item.value_unit].filter(v => v !== null && v !== undefined && v !== "").join(" · ")));
        if (item.value_condition) card.append(node("p", `Условие: ${item.value_condition}`));
        card.append(node("p", item.extraction_text), node("small", `Пункт ${item.numbering || "—"} · ${item.id}`));
      }
      if (item.breadcrumb) card.append(node("small", item.breadcrumb));
      if (type === "restrictions") {
        const button = node("button", "Подробнее и план проверки", "button");
        button.addEventListener("click", () => openRestriction(item.id));
        card.append(button);
      }
      $("#detail-items").append(card);
    }
    if (!$("#detail-items").children.length) $("#detail-items").append(node("p", type === "clauses" ? "Пунктов нет." : "Ограничения ещё не сохранены или не найдены при извлечении.", "empty"));
    itemsAfter = data.next_after || "";
    more.classList.toggle("hidden", !data.has_more);
  } catch (error) {
    if (sequence === itemsRequest) {
      if (!append) $("#detail-items").replaceChildren();
      $("#detail-items").append(node("p", error.message, "form-error"));
    }
  } finally {
    if (sequence === itemsRequest) more.disabled = false;
  }
}

function planTag(status) {
  const key = status || "none";
  return node("span", PLANS[key] || key, `tag ${key}`);
}

function valueText(item) {
  return [item.value_operator, item.value_number, item.value_unit].filter(v => v !== null && v !== undefined && v !== "").join(" ");
}

function fillSelect(select, items, value, label) {
  const current = select.value;
  select.replaceChildren(select.options[0]);
  for (const item of items) {
    const option = node("option", label(item));
    option.value = value(item);
    select.append(option);
  }
  // A chosen value that left the graph stays selected, so the filter keeps meaning the same.
  if (current && ![...select.options].some(option => option.value === current)) {
    const option = node("option", current);
    option.value = current;
    select.append(option);
  }
  select.value = current;
}

async function loadFacets() {
  try {
    const data = await request(`${API}/restrictions/facets`);
    fillSelect($("#r-doc"), data.documents, d => d.doc_id, d => `${d.name || d.doc_id} (${d.restrictions})`);
    fillSelect($("#r-kind"), data.kinds, k => k.value, k => `${k.value} (${k.restrictions})`);
    fillSelect($("#r-template"), data.templates, t => t.value, t => `${t.value} (${t.restrictions})`);
    const plans = Object.fromEntries(data.plans.map(p => [p.value, p.restrictions]));
    for (const option of $("#r-plan").options) {
      if (!option.value) continue;
      const count = option.value === "executable" ? (plans.auto || 0) + (plans.reviewed || 0) : plans[option.value] || 0;
      option.textContent = `${option.dataset.label} (${count})`;
    }
  } catch (error) { message(error.message, true); }
}

function restrictionRow(item) {
  const row = node("tr");
  const triple = node("td", `${item.subject || "—"} → ${item.object || "—"}`);
  triple.append(node("small", item.extraction_text));
  const kind = node("td", item.kind || "—");
  const value = valueText(item);
  if (value) kind.append(node("small", value));
  if (item.value_condition) kind.append(node("small", `Условие: ${item.value_condition}`));
  const doc = node("td", item.document_name || item.doc_id || "—");
  doc.append(node("small", `Пункт ${item.numbering || "—"}`));
  const plan = node("td");
  plan.append(planTag(item.plan_status));
  if (item.plan_template && item.plan_template !== item.plan_status) plan.append(node("small", item.plan_template));
  const action = node("td");
  const button = node("button", "Открыть", "button");
  button.addEventListener("click", () => openRestriction(item.id));
  action.append(button);
  row.append(triple, kind, doc, plan, action);
  return row;
}

async function loadRestrictions(append = false) {
  const sequence = ++restrictionsRequest;
  const more = $("#restrictions-more");
  more.disabled = true;
  if (!append) {
    restrictionFilter = {query: $("#r-query").value.trim(), doc_id: $("#r-doc").value, kind: $("#r-kind").value, plan: $("#r-plan").value, template: $("#r-template").value};
    restrictionsAfter = "";
    $("#restrictions-body").replaceChildren();
    $("#restrictions-summary").textContent = "Поиск ограничений…";
    $("#restrictions-empty").textContent = "Загрузка ограничений…";
    $("#restrictions-empty").classList.remove("hidden");
    more.classList.add("hidden");
  }
  try {
    const params = new URLSearchParams({...restrictionFilter, after: restrictionsAfter, limit: "50"});
    const data = await request(`${API}/restrictions?${params}`);
    if (sequence !== restrictionsRequest) return;
    for (const item of data.items) $("#restrictions-body").append(restrictionRow(item));
    if (data.total !== null && data.total !== undefined) {
      $("#restrictions-summary").textContent = `Найдено ограничений: ${Number(data.total).toLocaleString("ru-RU")}. В скобках у фильтров — число ограничений с этим значением во всём графе.`;
    }
    const empty = !$("#restrictions-body").children.length;
    $("#restrictions-empty").textContent = "Ограничения не найдены. Измените или сбросьте фильтры.";
    $("#restrictions-empty").classList.toggle("hidden", !empty);
    restrictionsAfter = data.next_after || "";
    more.classList.toggle("hidden", !data.has_more);
  } catch (error) {
    if (sequence === restrictionsRequest) {
      message(error.message, true);
      $("#restrictions-summary").textContent = "";
      $("#restrictions-empty").textContent = "Не удалось загрузить ограничения. Нажмите «Найти».";
    }
  } finally {
    if (sequence === restrictionsRequest) more.disabled = false;
  }
}

function showDocumentRestrictions(docId) {
  const select = $("#r-doc");
  if (![...select.options].some(option => option.value === docId)) {
    const option = node("option", docId);
    option.value = docId;
    select.append(option);
  }
  for (const id of ["r-query", "r-kind", "r-plan", "r-template"]) $(`#${id}`).value = "";
  select.value = docId;
  $("#document-dialog").close();
  showView("restrictions");
}

async function openRestriction(restrictionId) {
  const sequence = ++restrictionRequest;
  currentRestriction = null;
  $("#restriction-title").textContent = "Загрузка…";
  $("#restriction-metadata").replaceChildren();
  $("#restriction-plan").replaceChildren();
  $("#restriction-json").replaceChildren();
  $("#restriction-text").textContent = "";
  $("#restriction-clause").textContent = "";
  $("#restriction-document").disabled = true;
  if (!$("#restriction-dialog").open) $("#restriction-dialog").showModal();
  try {
    const data = await request(`${API}/restrictions/${encodeURIComponent(restrictionId)}`);
    if (sequence !== restrictionRequest) return;
    currentRestriction = data;
    const r = data.restriction;
    const clause = data.clause || {};
    const doc = data.document || {};
    const plan = data.plan;
    $("#restriction-title").textContent = `${r.subject || "—"} → ${r.object || "—"}`;
    metadata($("#restriction-metadata"), [
      ["Вид", r.kind_status ? `${r.kind} (${r.kind_status})` : r.kind],
      ["Значение", valueText(r) || null], ["Условие", r.value_condition],
      ["Документ", doc.name || r.doc_id], ["Пункт", clause.numbering], ["Раздел", clause.breadcrumb],
      ["ID ограничения", r.id],
    ]);
    $("#restriction-text").textContent = r.extraction_text || "—";
    $("#restriction-clause").textContent = clause.text || "Пункт не найден в графе.";
    metadata($("#restriction-plan"), plan ? [
      ["Статус", PLANS[plan.planner_status] || plan.planner_status],
      ["Шаблон", plan.template ? `${plan.template} v${plan.template_version ?? "—"}` : null],
      ["Ревизия", `${plan.revision} из ${data.plan_revisions}`], ["Статус проверки", plan.review_status],
      ["Автор", plan.author || "Планировщик"], ["Причина правки", plan.reason],
      ["Версия планировщика", plan.planner_version], ["Создан", plan.created_at],
    ] : [["Статус", "Плана нет: его создаст сверка при запуске или «Перестроить все планы»."]]);
    for (const [label, value] of [["Параметры плана", plan?.params], ["Требования к данным", plan?.declared_requirements], ["Источник плана", plan?.source], ["Показатель (measurement)", r.measurement]]) {
      if (value === null || value === undefined) continue;
      const details = node("details");
      details.append(node("summary", label), node("pre", JSON.stringify(value, null, 2), "json"));
      $("#restriction-json").append(details);
    }
    $("#restriction-document").disabled = !r.doc_id;
  } catch (error) {
    if (sequence === restrictionRequest) $("#restriction-title").textContent = error.message;
  }
}

function setBusy(busy) {
  operationRunning = busy;
  const blocked = busy || bulkRunning || bulkStarting;
  $("#sync-submit").disabled = blocked;
  $("#detail-sync").disabled = blocked || !currentDocument;
  $("#detail-extract").disabled = blocked || !currentDocument?.clauses;
  $("#reprocess-all").disabled = blocked || !bulkStatusKnown;
  $("#replan-all").disabled = blocked || !bulkStatusKnown;
}

function renderReprocessing(status) {
  bulkStatusKnown = true;
  bulkRunning = status.state === "running";
  const labels = {idle: "Массовая обработка не запускалась после старта сервиса.", running: "Выполняется", completed: "Завершено", completed_with_errors: "Завершено с ошибками или пропусками", failed: "Не удалось запустить обработку", interrupted: "Обработка прервана"};
  let text = labels[status.state] || "Неизвестное состояние";
  const replan = status.kind === "replan";
  if (replan) text += ` · Планы: ${status.processed}/${status.total} · С автопроверкой: ${status.auto} · Без автопроверки: ${status.unsupported} · Записано: ${status.written} · Ошибки: ${status.failed}`;
  else if (status.total !== undefined) text += ` · Документы: ${status.processed}/${status.total} · Успешно: ${status.succeeded} · Ошибки: ${status.failed} · Пропущено: ${status.skipped} · Извлечено норм: ${status.restrictions}`;
  if (status.current_document) text += ` · Сейчас: ${status.current_document.name || status.current_document.doc_id}`;
  // One job at a time: the other panel says whether it is blocked or was never run.
  const other = status.state === "idle" ? text : bulkRunning ? "Выполняется другое массовое задание." : "Не запускалось после старта сервиса.";
  $("#reprocess-status").textContent = replan ? other : text;
  $("#replan-status").textContent = replan ? text : other;
  const errors = $("#reprocess-errors");
  errors.replaceChildren();
  for (const item of status.errors || []) {
    const row = node("p", `${item.name || item.doc_id}: ${item.message}`, "form-error");
    const button = node("button", "Открыть документ", "button");
    button.addEventListener("click", () => openDocument(item.doc_id));
    row.append(button);
    errors.append(row);
  }
  if (status.errors_truncated) errors.append(node("p", "Показаны первые 100 ошибок. Остальные — в логах.", "muted"));
  setBusy(operationRunning);
}

async function loadReprocessing() {
  clearTimeout(bulkTimer);
  const requestId = ++bulkRequest;
  try {
    const status = await request(`${API}/reprocessing`);
    if (requestId !== bulkRequest) return;
    renderReprocessing(status);
  } catch (error) {
    if (requestId !== bulkRequest) return;
    bulkStatusKnown = false;
    $("#reprocess-status").textContent = `Статус недоступен. Это не означает остановку обработки. ${error.message}`;
    $("#replan-status").textContent = $("#reprocess-status").textContent;
    setBusy(operationRunning);
  } finally {
    if (requestId === bulkRequest) bulkTimer = setTimeout(loadReprocessing, 5000);
  }
}

async function startReprocessing() {
  await startBulk("reprocessing", "Пересоздать все нормы и планы во всех загруженных документах, включая пользовательские? После успешного извлечения прежние нормы документа будут заменены. Обработка может занять длительное время.");
}

async function startReplanning() {
  await startBulk("replanning", "Перестроить планы проверки всех извлечённых норм текущим планировщиком? Нормы не меняются, планы экспертов сохраняются. Обработка вызывает языковую модель и может занять длительное время.");
}

async function startBulk(path, question) {
  if (operationRunning || bulkRunning || bulkStarting || !bulkStatusKnown) return;
  if (!confirm(question)) return;
  bulkStarting = true;
  setBusy(operationRunning);
  clearTimeout(bulkTimer);
  ++bulkRequest;
  try {
    renderReprocessing(await request(`${API}/${path}`, {method: "POST", body: {}}));
  } catch (error) {
    message(error.message, true);
  } finally {
    bulkStarting = false;
    await loadReprocessing();
  }
}

function renderResult(result) {
  const results = Array.isArray(result) ? result : [result];
  const target = $("#operation-result");
  target.replaceChildren();
  if (!results.length) {
    target.append(node("p", "Документы с таким названием не найдены в IDU_DVD.", "notice"));
    return;
  }
  for (const item of results) {
    const card = node("article", null, "detail-item");
    card.append(node("h3", item.doc_id || item.name || "Результат"));
    const state = item.extraction_incomplete || item.incomplete ? "Извлечение не завершено" :
      item.skipped ? `Пропущено: ${item.reason || "см. логи"}` :
      item.extraction_skipped ? "Документ не изменился — использованы готовые нормы" : "Обработка завершена";
    card.append(node("p", state));
    card.append(node("p", `Пункты: ${item.clauses ?? item.clauses_processed ?? "—"} · Ограничения: ${item.restrictions ?? 0}`));
    for (const warning of item.warnings || []) card.append(node("p", warning, "form-error"));
    if (item.failed_clause_ids?.length) card.append(node("p", `Пункты с ошибками: ${item.failed_clause_ids.join(", ")}`, "form-error"));
    if (item.doc_id && !item.skipped) {
      const button = node("button", "Открыть документ", "button");
      button.addEventListener("click", () => openDocument(item.doc_id));
      card.append(button);
    }
    target.append(card);
  }
}

async function runOperation(url, body) {
  if (operationRunning || bulkRunning || bulkStarting) return;
  setBusy(true);
  $("#document-dialog").close();
  showView("operations");
  $("#operation-result").replaceChildren();
  $("#operation-status").textContent = "Выполняется обработка. Дождитесь результата; запрос может занять несколько минут.";
  message("");
  try {
    const result = await request(url, {method: "POST", body});
    renderResult(result);
    $("#operation-status").textContent = "Запрос завершён. Результат по документам ниже.";
    await Promise.all([loadOverview(), loadDocuments()]);
  } catch (error) {
    $("#operation-status").textContent = error.message;
    message(error.message, true);
  } finally {
    setBusy(false);
  }
}

const SETTINGS = {
  dvd_base_url: "Адрес IDU_DVD", llm_provider: "Провайдер LLM", llm_model: "Модель LLM",
  embeddings_model: "Модель эмбеддингов", vector_size: "Размерность вектора",
  extract_concurrency: "Параллельных пунктов", extraction_passes: "Проходов извлечения",
  entity_merge_threshold: "Порог объединения сущностей", kind_match_threshold: "Порог сопоставления видов",
  kafka_topic: "Топик Kafka", kafka_group_id: "Группа Kafka", reconcile_on_startup: "Сверка при запуске",
  check_plan_backfill_on_startup: "Создание недостающих планов при запуске",
};

async function loadSettings() {
  try {
    const data = await request(`${API}/settings`);
    metadata($("#settings-list"), Object.entries(data).map(([key, value]) => [SETTINGS[key] || key, typeof value === "boolean" ? (value ? "Да" : "Нет") : value]));
  } catch (error) { message(error.message, true); }
}

function showView(view) {
  $$(".view").forEach(el => el.classList.toggle("active", el.id === `view-${view}`));
  $$(".nav-link").forEach(el => el.classList.toggle("active", el.dataset.view === view));
  $("#page-title").textContent = {overview: "Обзор", documents: "Документы", restrictions: "Ограничения", operations: "Обработка", settings: "Настройки"}[view];
  if (view === "overview") loadOverview();
  if (view === "documents") loadDocuments();
  if (view === "restrictions") { loadFacets(); loadRestrictions(); }
  if (view === "settings") loadSettings();
}

function init() {
  try { document.documentElement.dataset.theme = localStorage.getItem("normgraph-theme") || "dark"; } catch (_) {}
  if (bindLogin()) return;
  $("#theme-toggle").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("normgraph-theme", theme); } catch (_) {}
  });
  $$("[data-view]").forEach(el => el.addEventListener("click", () => showView(el.dataset.view)));
  $$("[data-goto]").forEach(el => el.addEventListener("click", () => showView(el.dataset.goto)));
  $("#logout").addEventListener("click", async () => {
    try {
      await request("/admin/ui/logout", {method: "POST"});
      window.location.replace("/admin/ui/login");
    } catch (error) { message(error.message, true); }
  });
  $("#document-filters").addEventListener("submit", event => { event.preventDefault(); message(""); loadDocuments(); });
  $("#documents-more").addEventListener("click", () => loadDocuments(true));
  $("#close-detail").addEventListener("click", () => $("#document-dialog").close());
  $("#document-dialog").addEventListener("close", () => { ++detailRequest; ++itemsRequest; });
  $("#detail-refresh").addEventListener("click", () => { if (currentDocument) openDocument(currentDocument.doc_id); });
  $("#detail-more").addEventListener("click", () => loadItems(true));
  $("#detail-restrictions").addEventListener("click", () => { if (currentDocument) showDocumentRestrictions(currentDocument.doc_id); });
  $("#restriction-filters").addEventListener("submit", event => { event.preventDefault(); message(""); loadRestrictions(); });
  // The form resets its fields after the event: read them on the next tick.
  $("#restriction-filters").addEventListener("reset", () => setTimeout(() => { message(""); loadRestrictions(); }));
  $("#restrictions-more").addEventListener("click", () => loadRestrictions(true));
  $("#close-restriction").addEventListener("click", () => $("#restriction-dialog").close());
  $("#restriction-dialog").addEventListener("close", () => { ++restrictionRequest; });
  $("#restriction-document").addEventListener("click", () => {
    const docId = currentRestriction?.restriction.doc_id;
    if (!docId) return;
    $("#restriction-dialog").close();
    openDocument(docId);
  });
  $$("[data-collection]").forEach(el => el.addEventListener("click", () => {
    collection = el.dataset.collection;
    $$("[data-collection]").forEach(tab => tab.classList.toggle("active", tab === el));
    loadItems();
  }));
  $("#sync-form").addEventListener("submit", event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const body = {target: data.get("target").trim(), by: data.get("by"), replace: data.has("replace"), user_id: data.get("user_id").trim() || null, scenario_id: data.get("scenario_id").trim() || null};
    if (!!body.user_id !== !!body.scenario_id) { message("Укажите и пользователя, и сценарий либо оставьте оба поля пустыми.", true); return; }
    if (body.replace && !confirm("Пересоздать ограничения документа? После успешного извлечения прежние нормы будут заменены, устаревшие пункты удалены.")) return;
    runOperation(`${API}/sync`, body);
  });
  $("#detail-sync").addEventListener("click", () => {
    if (currentDocument && confirm("Загрузить актуальные пункты из IDU_DVD и при необходимости запустить извлечение?")) runOperation(`${API}/sync`, {target: currentDocument.doc_id});
  });
  $("#detail-extract").addEventListener("click", () => {
    if (currentDocument && confirm("Повторить извлечение по сохранённым пунктам? Это вызовет языковую модель и может занять несколько минут.")) runOperation(`${API}/documents/${encodeURIComponent(currentDocument.doc_id)}/extract`, {replace: false});
  });
  $("#reprocess-all").addEventListener("click", startReprocessing);
  $("#replan-all").addEventListener("click", startReplanning);
  $("#reprocess-refresh").addEventListener("click", loadReprocessing);
  setBusy(false);
  loadReprocessing();
  window.addEventListener("beforeunload", event => {
    if (operationRunning) { event.preventDefault(); event.returnValue = ""; }
  });
  showView("overview");
}

init();
