"use strict";
// Deliberately tiny deterministic DOM: tests production renderers and fetch payloads.
// This does not replace visual or real browser integration testing.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {Feed, Console, allowedControls} = require("./app.js");
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.listeners = {}; this.value = ""; this._text = ""; this.checked = false; this.disabled = false; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ""; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  querySelector(selector) { const key = selector.match(/\[name="(.+)"\]/)?.[1]; return walk(this).find(e => e.name === key); }
}
function walk(element) { return [element, ...element.children.flatMap(walk)]; }
class Document {
  constructor() { this.elements = new Map(); }
  createElement(tag) { return new Element(tag); }
  getElementById(id) { if (!this.elements.has(id)) this.elements.set(id, new Element("div")); return this.elements.get(id); }
}
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/replay.json"), "utf8"));
const doc = new Document();
const feed = new Feed(doc, doc.getElementById("transcript"), doc.getElementById("calls"));
feed.apply(fixture); feed.apply(fixture);
assert.equal(feed.utterances.size, 4); assert.equal(feed.callIds.size, 2); assert.equal(feed.cursor, 8);
assert.equal(doc.getElementById("transcript").children.length, 4);
assert.equal(doc.getElementById("calls").children.length, 2);
assert.match(doc.getElementById("calls").textContent, /Unattributed caller/);
assert.match(doc.getElementById("calls").textContent, /Arguments redacted/);
assert.match(doc.getElementById("transcript").textContent, /<img src=x/);
assert(!walk(doc.getElementById("transcript")).some(e => e.tagName === "img"));
feed.apply({events:[{event_id:9,type:"utterance",data:{utterance_id:4}}, {event_id:10,type:"new_event"}],next_after_id:10});
assert.equal(feed.utterances.size, 4); assert.equal(feed.cursor, 10);
feed.apply({events:[],next_after_id:1}); assert.equal(feed.cursor, 10);
const granted = {allowed:true,state:"active",scopes:{local_processing:true,openai_audio:true,hosted_mcp:true}};
assert(allowedControls(granted,{session_id:"room"},"room"));
for (const consent of [null, {...granted,allowed:false}, {...granted,state:"destroying"}, {...granted,scopes:{local_processing:true}}, {...granted,scopes:{...granted.scopes,openai_audio:false}}]) assert(!allowedControls(consent,{session_id:"room"},"room"));
assert(!allowedControls(granted,null,"room")); assert(!allowedControls(granted,{session_id:"other"},"room"));

async function main() {
  const calls = [];
  const fetcher = async (url, options) => {
    calls.push({url,options});
    let data = {};
    if (url === "/privacy/notice") data = {configured:true,controller_name:"Test Controller",controller_address:"Test address",controller_email:"test@example.invalid",notice_text:"Synthetic collection notice",notice_sha256:"notice_hash",retention_text:"Current room only",vendors:{},policy_version:"test",consent_method_version:"test"};
    if (url.endsWith("/challenge")) data = {challenge:"single_use",notice_sha256:"notice_hash",expires_at_ms:Date.now()+10000};
    return {ok:true,status:200,json:async()=>data};
  };
  const ui = new Console(new Document(), fetcher, {hostname:"127.0.0.1",protocol:"http:"});
  ui.token = "synthetic_operator_credential"; ui.session = "room";
  await ui.loadNotice(); assert(ui.noticeReady); assert(!calls[0].options.headers.Authorization);
  ui.consent = {session_id:"room",state:"pending",allowed:false,participants:[{id:"person_1",name:"Synthetic Person",bipa_consent_granted:false}]};
  ui.renderConsent();
  const person = ui.$("consents").children[0];
  const checkboxes = walk(person).filter(e=>e.type==="checkbox");
  assert.equal(checkboxes.length,3); assert(checkboxes.every(e=>!e.checked));
  assert(!walk(person).some(e => e.tagName === "input" && e.value === "Synthetic Person"));
  const submit = walk(person).find(e=>e.tagName==="button"); assert(!submit.disabled);
  ui.poll = async()=>{};
  await ui.sign("person_1","Synthetic Person",["openai_audio"]);
  const grantRequest = calls.at(-1);
  assert.equal(grantRequest.url,"/speaker/session/room/consents/person_1");
  assert.deepEqual(JSON.parse(grantRequest.options.body),{challenge:"single_use",notice_sha256:"notice_hash",signature_text:"Synthetic Person",accepted:true,disclosure_scopes:["openai_audio"]});
  assert.equal(grantRequest.options.headers.Authorization,"Bearer synthetic_operator_credential");
  assert.equal(grantRequest.options.cache,"no-store"); assert.equal(grantRequest.options.redirect,"error");
  const count = calls.length; ui.noticeReady = false;
  await assert.rejects(()=>ui.sign("person_1","Synthetic Person",[]),/current notice/); assert.equal(calls.length,count);
  ui.noticeReady = true; ui.notice.notice_sha256 = "changed_notice";
  await assert.rejects(()=>ui.sign("person_1","Synthetic Person",[]),/notice changed/); assert(calls.at(-1).url.endsWith("/challenge"));
  ui.feed.apply(fixture); await ui.revoke("person_1"); assert.equal(ui.feed.utterances.size,0); assert.equal(ui.feed.callIds.size,0);
  assert.equal(calls.at(-1).url,"/speaker/session/room/consents/person_1/revoke");
  ui.feed.apply(fixture); ui.disconnect(); assert.equal(ui.token,""); assert.equal(ui.feed.utterances.size,0);

  const preEnrollment = new Console(new Document(), async (url) => {
    if (url.endsWith("/consent")) return {ok:true,status:200,json:async()=>({session_id:"room",state:"active",allowed:true,participants:[],scopes:{local_processing:true}})};
    if (url.includes("/events?")) return {ok:false,status:404,json:async()=>({message:"Session does not exist."})};
    return {ok:true,status:200,json:async()=>({})};
  }, {hostname:"127.0.0.1",protocol:"http:"});
  preEnrollment.token = "synthetic_operator_credential"; preEnrollment.session = "room";
  await preEnrollment.poll();
  assert.match(preEnrollment.$("feed-state").textContent,/Awaiting runtime enrollment/);
  assert.match(preEnrollment.$("runtime-state").textContent,/Start enrollment/);

  const noConfig = new Console(new Document(), async()=>({ok:true,status:200,json:async()=>({configured:false})}), {hostname:"127.0.0.1",protocol:"http:"});
  await noConfig.loadNotice(); assert(!noConfig.noticeReady); assert(noConfig.$("create-room").disabled);

  // Launcher-started runtime: bootstrap hands over the token, the page drives setup, enrollment, start and stop.
  const runtimeCalls = [];
  let phase = "setup", awaiting = null, sessionId = null;
  const runtimeFetcher = async (url, options) => {
    runtimeCalls.push({url, options});
    let data = {};
    if (url === "http://127.0.0.1:8123/bootstrap") { assert(!options.headers.Authorization); data = {phase, session_id:sessionId, gui:true, api_token:"launcher_token"}; }
    else if (url === "http://127.0.0.1:8123/agents") data = {session_id:sessionId, agents:[], phase, detail:`in ${phase}`, awaiting, needs_openai_key:phase === "setup",
      participants: sessionId ? [{id:"participant_1", name:"Synthetic One", enrollment:{state:awaiting === "participant_1" ? "waiting" : "recorded", peak:awaiting ? null : 9000}}] : []};
    else if (url === "http://127.0.0.1:8123/setup") { phase = "consent"; sessionId = "room_auto"; }
    else if (url === "http://127.0.0.1:8123/enrollment/record") { awaiting = null; phase = "ready"; }
    else if (url === "http://127.0.0.1:8123/start") phase = "live";
    else if (url === "http://127.0.0.1:8123/stop") phase = "ended";
    else if (url === "/privacy/notice") data = {configured:true,controller_name:"Test Controller",controller_address:"Test address",controller_email:"test@example.invalid",notice_text:"Synthetic",notice_sha256:"h",retention_text:"r",vendors:{},policy_version:"t",consent_method_version:"t"};
    else if (url === "/speaker/sessions?limit=100") data = {sessions:[]};
    else if (url === "/privacy/rooms") data = {rooms:[]};
    else if (url.endsWith("/consent")) data = {session_id:"room_auto",state:"pending",allowed:false,participants:[]};
    return {ok:true,status:200,json:async()=>data};
  };
  const launched = new Console(new Document(), runtimeFetcher, {hostname:"127.0.0.1",protocol:"http:",search:"?control=8123"});
  assert.equal(launched.control, "http://127.0.0.1:8123");
  assert.equal(new Console(new Document(), runtimeFetcher, {hostname:"127.0.0.1",protocol:"http:",search:"?control=evil"}).control, "http://127.0.0.1:8090");
  await launched.bootstrap(); assert.equal(launched.token, "launcher_token");
  await launched.pollRuntime();
  assert.equal(launched.$("phase").textContent, "setup"); assert(!launched.$("setup-form").hidden); assert(!launched.$("key-label").hidden);
  launched.$("openai-key").value = "sk-synthetic-key-value-0000000000";
  launched.addSetupPerson(); launched.addSetupPerson(); const [rowOne, rowTwo] = launched.$("setup-roster").children;
  rowOne.querySelector('[name="name"]').value = "Synthetic One"; rowOne.querySelector('[name="contact"]').value = "one@example.invalid";
  await assert.rejects(() => launched.submitSetup(), /two to four people/);          // one person is not a room
  rowTwo.querySelector('[name="name"]').value = "Synthetic Two"; rowTwo.querySelector('[name="contact"]').value = "555-0100";
  launched.$("openai-key").value = "sk-synthetic-key-value-0000000000";
  await launched.submitSetup();
  const setup = runtimeCalls.find(c => c.url.endsWith("/setup"));
  assert.deepEqual(JSON.parse(setup.options.body), {participants:[{name:"Synthetic One",contact:"one@example.invalid"},{name:"Synthetic Two",contact:"555-0100"}], openai_api_key:"sk-synthetic-key-value-0000000000"});
  assert.equal(setup.options.headers.Authorization, "Bearer launcher_token");
  assert.equal(launched.$("openai-key").value, "");                       // key never lingers in the page
  assert.equal(launched.session, "room_auto");                            // runtime's room opened automatically
  assert(launched.$("setup-form").hidden);
  phase = "enrollment"; awaiting = "participant_1"; await launched.pollRuntime();
  const recordButton = walk(launched.$("enrollment")).find(e => e.tagName === "button");
  assert.match(recordButton.textContent, /Record Synthetic One now/); assert(!recordButton.disabled); assert(launched.$("start").hidden);
  await launched.recordParticipant("participant_1");
  assert.deepEqual(JSON.parse(runtimeCalls.find(c => c.url.endsWith("/enrollment/record")).options.body), {participant_id:"participant_1"});
  assert.equal(launched.$("phase").textContent, "ready"); assert(!launched.$("start").hidden);
  assert.match(launched.$("enrollment").textContent, /peak 9000/);
  await launched.runtimeAction("/start"); assert.equal(launched.$("phase").textContent, "live"); assert(launched.$("start").hidden);
  assert.match(launched.$("stop-runtime").textContent, /End conversation/);
  await launched.runtimeAction("/stop"); assert.equal(launched.$("phase").textContent, "ended"); assert(launched.$("stop-runtime").hidden);
  const manual = new Console(new Document(), async (url) => { if (url.endsWith("/bootstrap")) throw new Error("no runtime"); return {ok:true,status:200,json:async()=>({})}; }, {hostname:"127.0.0.1",protocol:"http:"});
  await manual.bootstrap(); assert.equal(manual.token, "");               // no launcher: paste the token as before
  console.log("DOM/mock API checks passed: 4 replay rows, 2 calls, dedupe, XSS text, effective gates, unchecked releases, nonce binding, withdrawal clearing.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
