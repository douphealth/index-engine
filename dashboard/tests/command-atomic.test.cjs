"use strict";
const test = require("node:test"), assert = require("node:assert/strict");
const { DatabaseSync } = require("node:sqlite");
function database() {
  const sql = new DatabaseSync(":memory:");
  sql.exec("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE commands(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT,kind TEXT,options TEXT,status TEXT DEFAULT 'pending',picked_at TEXT,finished_at TEXT,result TEXT);");
  return { sql, DB: { prepare(query) {
    let params = [];
    const stmt = { bind(...args) { params = args; return stmt; }, async first() { return sql.prepare(query).get(...params) || null; }, async run() { const r = sql.prepare(query).run(...params); return { meta: { changes: Number(r.changes), last_row_id: Number(r.lastInsertRowid) } }; } };
    return stmt;
  } } };
}
async function call(db, body) {
  const { onRequestPost } = await import("../functions/api/command.js");
  const response = await onRequestPost({ env: db, request: new Request("https://example.test/api/command", { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } }) });
  return { status: response.status, body: await response.json() };
}
test("concurrent queue requests create exactly one command", async () => {
  const db = database(); try {
    const results = await Promise.all(Array.from({length: 12}, () => call(db, {kind:"run",options:{no_submit:true}})));
    assert.equal(db.sql.prepare("SELECT COUNT(*) AS n FROM commands").get().n, 1);
    assert.equal(results.filter(r => r.body.id).length, 1);
    assert.equal(results.filter(r => r.body.command).length, 11);
  } finally { db.sql.close(); }
});
test("measure-only options survive queue persistence", async () => { const db=database(); try { await call(db,{kind:"run",options:{site:"example.com",no_submit:true,force_audit:true}}); const row=db.sql.prepare("SELECT options FROM commands").get(); assert.deepEqual(JSON.parse(row.options),{site:"example.com",no_submit:true,force_audit:true}); } finally {db.sql.close();} });
test("paused website is rejected before insertion", async () => { const db=database(); try {db.sql.prepare("INSERT INTO settings VALUES(?,?)").run("sites_disabled",'["example.com"]');const r=await call(db,{kind:"run",options:{site:"example.com"}});assert.equal(r.status,409);assert.equal(db.sql.prepare("SELECT COUNT(*) AS n FROM commands").get().n,0);}finally{db.sql.close();} });
test("malformed bodies and unsafe boolean types return 400", async () => {const db=database();try{for(const body of [null,[],{kind:"run",options:null},{kind:"run",options:{no_submit:"true"}},{cancel:-1}])assert.equal((await call(db,body)).status,400);}finally{db.sql.close();}});
test("doctor requires recent capability evidence, including when fields are malformed", async () => {const db=database();try{for(const seen of [{},null,{at:new Date().toISOString(),capabilities:"doctor"},{at:"broken",capabilities:["doctor"]},{at:"2020-01-01T00:00:00Z",capabilities:["doctor"]}]){db.sql.prepare("INSERT OR REPLACE INTO settings VALUES(?,?)").run("engine_last_seen",JSON.stringify(seen));assert.equal((await call(db,{kind:"doctor"})).status,409);}}finally{db.sql.close();}});
test("doctor may queue for a recently compatible engine", async()=>{const db=database();try{db.sql.prepare("INSERT INTO settings VALUES(?,?)").run("engine_last_seen",JSON.stringify({at:new Date().toISOString(),capabilities:["doctor"]}));const r=await call(db,{kind:"doctor"});assert.equal(r.status,200);assert.equal(r.body.kind,"doctor");assert.equal(r.body.status,"pending");}finally{db.sql.close();}});
test("cancelling a pending command releases the queue",async()=>{const db=database();try{const first=await call(db,{kind:"run"});assert.equal((await call(db,{cancel:first.body.id})).body.ok,true);assert.ok((await call(db,{kind:"run"})).body.id);}finally{db.sql.close();}});
