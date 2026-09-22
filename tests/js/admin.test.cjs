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
