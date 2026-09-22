const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor() {
    this.children = []; this.textContent = ""; this.value = "";
    this.classList = {add() {}, remove() {}, toggle() {}};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener() {}
  set innerHTML(_) { throw new Error("Untrusted data must not become HTML"); }
}

const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});

function app(fetch) {
  const elements = new Map();
  const get = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const context = vm.createContext({
    document: {querySelector: get, createElement: () => new Element()},
    URLSearchParams, fetch,
    window: {location: {replace(url) { context.redirect = url; }}},
  });
  const source = fs.readFileSync(path.join(__dirname, "../../src/admin_service/static/admin.js"), "utf8");
  vm.runInContext(source.replace(/\ninit\(\);\s*$/, ""), context);
  return {context, get, run: (code) => vm.runInContext(code, context)};
}

test("expired admin session returns to login without interpreting a response as data", async () => {
  const ui = app(async () => response({detail: "expired"}, 401));
  await assert.rejects(ui.run('request(API + "/documents")'), /Сессия/);
  assert.equal(ui.context.redirect, "/admin/ui/login");
});

test("login errors stay on the login page and requests carry the CSRF header", async () => {
  let options;
  const ui = app(async (_url, value) => { options = value; return response({detail: "Неверный пароль"}, 401); });
  await assert.rejects(ui.run('request("/admin/ui/session", {method:"POST", body:{username:"test"}})'), /Неверный пароль/);
  assert.equal(ui.context.redirect, undefined);
  assert.equal(options.headers["X-NormGraph-Admin"], "1");
  assert.equal(options.credentials, "same-origin");
});

test("a late document response cannot overwrite a newer search", async () => {
  const pending = [];
  const ui = app(() => new Promise(resolve => pending.push(resolve)));
  ui.get("#doc-query").value = "old";
  const old = ui.run("loadDocuments()");
  ui.get("#doc-query").value = "new";
  const newer = ui.run("loadDocuments()");
  const item = {doc_id:"new", name:"<img src=x onerror=alert(1)>", state:"unknown", clauses:1, restrictions:0};
  pending[1](response({items:[item], has_more:false}));
  await newer;
  pending[0](response({items:[{...item, doc_id:"old", name:"old"}], has_more:false}));
  await old;
  const rows = ui.get("#documents-body").children;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].children[0].textContent, item.name);
});

test("partial extraction is presented as incomplete, including failed clause IDs", () => {
  const ui = app(async () => response({}));
  ui.run('renderResult({doc_id:"d", restrictions:4, extraction_incomplete:true, failed_clause_ids:["c1"]})');
  const card = ui.get("#operation-result").children[0];
  assert.equal(card.children[1].textContent, "Извлечение не завершено");
  assert.ok(card.children.some(el => el.textContent.includes("c1")));
});

test("a lost connection does not claim the server stopped processing", async () => {
  const ui = app(async () => { throw new Error("offline"); });
  await assert.rejects(ui.run('request(API + "/sync", {method:"POST"})'), /проверьте статус документа перед повтором/);
});

test("bulk progress disables mutations and renders errors as text", () => {
  const ui = app(async () => response({}));
  ui.run('renderReprocessing({state:"running", total:3, processed:1, succeeded:0, failed:1, skipped:0, restrictions:0, current_document:{name:"<script>bad</script>"}, errors:[{doc_id:"d",name:"<img>",message:"failed"}]})');
  assert.equal(ui.get("#reprocess-all").disabled, true);
  assert.equal(ui.get("#sync-submit").disabled, true);
  assert.match(ui.get("#reprocess-status").textContent, /1\/3/);
  assert.match(ui.get("#reprocess-status").textContent, /<script>/);
  assert.match(ui.get("#reprocess-errors").children[0].textContent, /<img>/);
  ui.run('renderReprocessing({state:"completed_with_errors",total:3,processed:3,succeeded:2,failed:1,skipped:0,restrictions:4})');
  assert.equal(ui.get("#reprocess-all").disabled, false);
  assert.match(ui.get("#reprocess-status").textContent, /Завершено с ошибками/);
});

test("lost progress response does not unlock a running job", async () => {
  const ui = app(async () => { throw new Error("offline"); });
  ui.context.clearTimeout = () => {};
  ui.context.setTimeout = () => 1;
  ui.run('renderReprocessing({state:"running"})');
  await ui.run("loadReprocessing()");
  assert.equal(ui.get("#reprocess-all").disabled, true);
  assert.equal(ui.get("#sync-submit").disabled, true);
  assert.match(ui.get("#reprocess-status").textContent, /не означает остановку/);
});

test("bulk start sends a single request and restores server progress", async () => {
  let starts = 0;
  const ui = app(async (_url, options) => {
    if (options.method === "POST") starts++;
    return response({state:"running",total:2,processed:0,succeeded:0,failed:0,skipped:0,restrictions:0});
  });
  ui.context.confirm = () => true;
  ui.context.clearTimeout = () => {};
  ui.context.setTimeout = () => 1;
  ui.run('renderReprocessing({state:"idle"})');
  await ui.run("Promise.all([startReprocessing(), startReprocessing()])");
  assert.equal(starts, 1);
  assert.equal(ui.get("#reprocess-all").disabled, true);
});
