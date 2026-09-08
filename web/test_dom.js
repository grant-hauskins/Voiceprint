"use strict";
// Deliberately tiny deterministic DOM: tests production renderers and fetch payloads.
// This does not replace visual or real browser integration testing.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {Feed, Console, allowedControls, parseObjectiveFile} = require("./app.js");
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.listeners = {}; this.value = ""; this._text = ""; this.checked = false; this.disabled = false; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ""; }
  setAttribute(key, value) { this[key] = value; }
  getAttribute(key) { return this[key]; }
  prepend(...nodes) { this.children.unshift(...nodes); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  querySelector(selector) { const key = selector.match(/\[name="(.+)"\]/)?.[1]; return walk(this).find(e => e.name === key); }
}
function walk(element) { return [element, ...element.children.flatMap(walk)]; }
class Document {
  constructor() { this.elements = new Map(); }
  createElement(tag) { return new Element(tag); }
  getElementById(id) { if (!this.elements.has(id)) this.elements.set(id, new Element("div")); return this.elements.get(id); }
}
const rows = el => el.children.filter(e => e.className !== "empty");
const buttons = el => walk(el).filter(e => e.tagName === "button");
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/replay.json"), "utf8"));
const doc = new Document();
const feed = new Feed(doc, doc.getElementById("transcript"), doc.getElementById("calls"), doc.getElementById("board"));
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
// v3: board rows come from agent_channel events (deduped by row_id); a raw-tier event never reaches the board.
const boardEvent = id => ({event_id:id,type:"agent_channel",data:{row_id:1,sender_participant_id:"agent_3",sender_name:"Mediator",tier:"board",tag:"REFOCUS_NEEDED",text:"Open: price ([withheld]). <b>not markup</b>",redactions:1,timestamp_ms:1780000007000}});
feed.apply({events:[boardEvent(11), boardEvent(12), {event_id:13,type:"agent_channel",data:{row_id:2,sender_name:"Ava",tier:"raw",text:"private raw note",redactions:0}}],next_after_id:13});
assert.equal(feed.boardIds.size, 1); assert.equal(doc.getElementById("board").children.length, 1); assert.equal(feed.cursor, 13);
const boardText = doc.getElementById("board").textContent;
assert.match(boardText, /Mediator/); assert.match(boardText, /REFOCUS_NEEDED/); assert.match(boardText, /1 withheld/); assert.doesNotMatch(boardText, /private raw note/);
assert(walk(doc.getElementById("board")).some(e => e.className === "chip withheld") && walk(doc.getElementById("board")).some(e => e.className === "chip tag"));
assert(!walk(doc.getElementById("board")).some(e => e.tagName === "b"));
feed.reset(); assert.equal(feed.boardIds.size, 0); assert.equal(doc.getElementById("board").children[0].className, "empty");
assert.equal(new Feed(doc, doc.getElementById("t2"), doc.getElementById("c2")).board.tagName, "ol");   // board element optional
const granted = {allowed:true,state:"active",scopes:{local_processing:true,openai_audio:true,hosted_mcp:true}};
assert(allowedControls(granted,{session_id:"room"},"room"));
for (const consent of [null, {...granted,allowed:false}, {...granted,state:"destroying"}, {...granted,scopes:{local_processing:true}}, {...granted,scopes:{...granted.scopes,openai_audio:false}}]) assert(!allowedControls(consent,{session_id:"room"},"room"));
assert(!allowedControls(granted,null,"room")); assert(!allowedControls(granted,{session_id:"other"},"room"));
// v3: uploaded objective files are parsed on this computer into fields; nothing else from the file is kept.
assert.deepEqual(parseObjectiveFile('{"position":" Sell the house ","constraints":[{"label":"floor","value":"300000"},{"label":"","value":"dropped"}],"other":"ignored"}', "offer.json"),
  {position:"Sell the house", constraints:[{label:"floor", value:"300000"}]});
assert.deepEqual(parseObjectiveFile("Sell the house quickly.\nfloor: 300000\nclose by: June 30\n\nPrefer a cash buyer.", "terms.txt"),
  {position:"Sell the house quickly.\nPrefer a cash buyer.", constraints:[{label:"floor", value:"300000"}, {label:"close by", value:"June 30"}]});
assert.throws(() => parseObjectiveFile("[1,2]", "list.json"), /object with position/);

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
  assert.equal(checkboxes.length,4); assert(checkboxes.every(e=>!e.checked));                       // v3: negotiation_text is the fourth optional scope
  assert.match(person.textContent, /negotiation_text disclosure/); assert.match(person.textContent, /negotiation rooms need all three/);
  assert(!walk(person).some(e => e.tagName === "input" && e.value === "Synthetic Person"));
  const submit = walk(person).find(e=>e.tagName==="button"); assert(!submit.disabled);
  ui.poll = async()=>{};
  // Signing through the form with every optional box checked sends all three scopes, in contract order.
  walk(person).find(e => e.tagName === "input" && e.type !== "checkbox").value = "Synthetic Person";
  for (const box of checkboxes) box.checked = true;
  await walk(person).find(e => e.tagName === "form").listeners.submit({preventDefault(){}});
  assert.match(walk(person).find(e => e.className === "release-feedback").textContent, /Written release recorded/);
  const grantRequest = calls.at(-1);
  assert.equal(grantRequest.url,"/speaker/session/room/consents/person_1");
  assert.deepEqual(JSON.parse(grantRequest.options.body),{challenge:"single_use",notice_sha256:"notice_hash",signature_text:"Synthetic Person",accepted:true,disclosure_scopes:["openai_audio","hosted_mcp","negotiation_text"]});
  assert.equal(grantRequest.options.headers.Authorization,"Bearer synthetic_operator_credential");
  assert.equal(grantRequest.options.cache,"no-store"); assert.equal(grantRequest.options.redirect,"error"); assert.equal(grantRequest.options.method,"POST");
  await ui.sign("person_1","Synthetic Person",["openai_audio"]);
  assert.deepEqual(JSON.parse(calls.at(-1).options.body).disclosure_scopes,["openai_audio"]);
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
  let phase = "setup", awaiting = null, sessionId = null, reviewedVendors = {openai_reviewed:false, cloudflare_reviewed:true};
  let convType = "casual", granted = false, revealed = false, forbidObjectives = false, objectiveVersions = {}, summary = null;
  const rawRows = [{row_id:1,sender_participant_id:"agent_1",sender_name:"Ava",tier:"raw",tag:null,text:"Ava to mediator: my side can move on timing.",redactions:0,timestamp_ms:1780000008000},
    {row_id:2,sender_participant_id:"agent_3",sender_name:"Mediator",tier:"raw",tag:"OBJECTIVE_ACHIEVED",text:"Zone found; price [withheld].",redactions:1,timestamp_ms:1780000009000}];
  const roomAgents = () => phase === "live" || phase === "ended" ? [
    {name:"Ava", role:"voice", participant_id:"agent_1", provider:"p", model:"m", voice:"marin", eagerness:"balanced", held:false, responding:true},
    {name:"Ben", role:"voice", participant_id:"agent_2", provider:"p", model:"m", voice:"cedar", eagerness:"quiet", held:true, responding:false},
    {name:"Mediator", role:"arbitrator", participant_id:"agent_3", provider:"openai_responses", model:"gpt-5", held:false, responding:false, arbitrator:{generations:arbGenerations, ingested_rows:12, last_trigger:"contribution", pending_tag:null, cooldown_until_ms:0, paused:false}}] : [];
  let arbGenerations = 3;
  const runtimeFetcher = async (url, options) => {
    runtimeCalls.push({url, options});
    let data = {};
    if (url === "http://127.0.0.1:8123/bootstrap") { assert(!options.headers.Authorization); data = {phase, session_id:sessionId, gui:true, api_token:"launcher_token"}; }
    else if (url === "http://127.0.0.1:8123/agents") data = {session_id:sessionId, agents:roomAgents(), phase, detail:`in ${phase}`, awaiting, needs_openai_key:phase === "setup", conversation_type:convType,
      agent_configs:[{name:"Ava", role:"voice", voice:"marin", model:"m", eagerness:"balanced", instructions:"Answer in haiku.", speaks_for:""}, {name:"Ben", voice:"cedar", model:"m", eagerness:"quiet", instructions:"", speaks_for:""}],
      voices:["marin","cedar","sage"], eagerness_levels:["quiet","balanced","eager"], max_agents:3,
      participants: sessionId ? [{id:"participant_1", name:"Synthetic One", enrollment:{state:awaiting === "participant_1" ? "waiting" : "recorded", peak:awaiting ? null : 9000}}] : []};
    else if (url === "http://127.0.0.1:8123/setup") { phase = "consent"; sessionId = "room_auto"; }
    else if (url === "http://127.0.0.1:8123/enrollment/record") { awaiting = null; phase = "ready"; }
    else if (url === "http://127.0.0.1:8123/start") phase = "live";
    else if (url === "http://127.0.0.1:8123/stop") phase = "ended";
    else if (url === "/privacy/notice") data = {configured:true,controller_name:"Test Controller",controller_address:"Test address",controller_email:"test@example.invalid",notice_text:"Synthetic",notice_sha256:"h",retention_text:"r",vendors:reviewedVendors,policy_version:"t",consent_method_version:"t"};
    else if (url === "/speaker/sessions?limit=100") data = {sessions:[]};
    else if (url === "/privacy/rooms") data = {rooms:[]};
    else if (url.endsWith("/consent")) data = granted ? {session_id:"room_auto",state:"active",allowed:true,participants:[{id:"participant_1",name:"Synthetic One",bipa_consent_granted:true},{id:"participant_2",name:"Synthetic Two",bipa_consent_granted:true}],scopes:{local_processing:true,openai_audio:true,hosted_mcp:true,negotiation_text:true}}
      : {session_id:"room_auto",state:"pending",allowed:false,participants:[]};
    else if (url.includes("/events?")) data = fixture;
    else if (url.includes("/agent_channel?")) data = {session_id:"room_auto", next_after_id:2, revealed, rows: revealed ? rawRows : []};   // the API omits raw rows until everyone revealed
    else if (url.endsWith("/agent_channel/reveal")) { const body = JSON.parse(options.body); revealed = body.revealed; data = {session_id:"room_auto", revealed_by: revealed ? [body.participant_id] : [], revealed:false}; }
    else if (url.endsWith("/objectives")) {
      if (forbidObjectives) return {ok:false,status:403,json:async()=>({error:"prior_written_release_required",message:"negotiation_text scope missing"})};
      const body = JSON.parse(options.body); objectiveVersions[body.principal_id] = (objectiveVersions[body.principal_id] || 0) + 1;
      data = {session_id:"room_auto", principal_id:body.principal_id, version:objectiveVersions[body.principal_id], created_ms:1780000050000};
    }
    else if (url.endsWith("/summary")) {
      if (options.method === "DELETE") { summary = null; data = {session_id:"room_auto", deleted:true}; }
      else if (summary) data = summary;
      else return {ok:false,status:404,json:async()=>({error:"not_found",message:"No summary"})};
    }
    return {ok:true,status:200,json:async()=>data};
  };
  const launched = new Console(new Document(), runtimeFetcher, {hostname:"127.0.0.1",protocol:"http:",search:"?control=8123"});
  assert.equal(launched.control, "http://127.0.0.1:8123");
  assert.equal(new Console(new Document(), runtimeFetcher, {hostname:"127.0.0.1",protocol:"http:",search:"?control=evil"}).control, "http://127.0.0.1:8090");
  await launched.bootstrap(); assert.equal(launched.token, "launcher_token");
  await launched.pollRuntime();
  assert.equal(launched.$("phase").textContent, "setup"); assert(!launched.$("setup-form").hidden); assert(!launched.$("key-label").hidden);
  assert(!launched.$("gate").hidden); assert(launched.$("gate-close").hidden); assert(!launched.$("room-section").hidden);   // inescapable setup gate
  assert.equal(launched.$("mic").getAttribute("data-state"), "off");
  assert.equal(launched.$("conversation-type").value, "casual");                                   // prefilled from the runtime
  // Unreviewed vendor settings block room creation up front instead of failing after everyone has signed.
  assert(launched.$("setup-submit").disabled); assert.match(launched.$("setup-blocked").textContent, /VOICEPRINT_OPENAI_REVIEWED=true/);
  await assert.rejects(() => launched.submitSetup(), /vendor review flags/);
  reviewedVendors = {openai_reviewed:true, cloudflare_reviewed:true}; await launched.loadNotice(); await launched.pollRuntime();
  assert(!launched.$("setup-submit").disabled); assert.equal(launched.$("setup-blocked").textContent, "");
  launched.$("openai-key").value = "sk-synthetic-key-value-0000000000";
  launched.addSetupPerson(); launched.addSetupPerson(); const [rowOne, rowTwo] = launched.$("setup-roster").children;
  rowOne.querySelector('[name="name"]').value = "Synthetic One"; rowOne.querySelector('[name="contact"]').value = "one@example.invalid";
  await assert.rejects(() => launched.submitSetup(), /two to four people/);          // one person is not a room
  rowTwo.querySelector('[name="name"]').value = "Synthetic Two"; rowTwo.querySelector('[name="contact"]').value = "555-0100";
  launched.$("openai-key").value = "sk-synthetic-key-value-0000000000";
  const agentCards = launched.$("setup-agents").children;
  assert.equal(agentCards.length, 2); assert.equal(agentCards[0].querySelector('[name="instructions"]').value, "Answer in haiku.");   // prefilled from the runtime
  assert.equal(agentCards[0].querySelector('[name="role"]').value, "voice"); assert.equal(agentCards[1].querySelector('[name="role"]').value, "voice");   // role defaults to voice
  assert(!agentCards[0].querySelector('[name="voice"]').parentLabel.hidden); assert(agentCards[0].querySelector('[name="role_note"]').hidden);
  agentCards[1].querySelector('[name="speaks_for"]').value = "Nobody Here";
  await assert.rejects(() => launched.submitSetup(), /speak for a person on the roster/);
  agentCards[1].querySelector('[name="speaks_for"]').value = "Synthetic Two"; agentCards[1].querySelector('[name="instructions"]').value = "Only words starting with A.";
  launched.$("openai-key").value = "sk-synthetic-key-value-0000000000";
  await launched.pollRuntime(); assert.equal(launched.$("setup-agents").children[1].querySelector('[name="instructions"]').value, "Only words starting with A.");   // polling keeps typed text
  launched.addAgentCard(); launched.addAgentCard();
  assert.equal(launched.$("setup-agents").children.length, 3); assert.match(launched.$("message").textContent, /At most 3 agents/);   // runtime's cap
  const third = launched.$("setup-agents").children[2];
  third.querySelector('[name="name"]').value = "Synthetic Two";                                          // collides with a human
  await assert.rejects(() => launched.submitSetup(), /distinct from the other agents and from the people/);
  third.querySelector('[name="name"]').value = "Cy"; third.querySelector('[name="voice"]').value = "sage"; third.querySelector('[name="eagerness"]').value = "eager";
  third.querySelector('[name="speaks_for"]').value = "Synthetic One"; third.querySelector('[name="instructions"]').value = "Speak only in questions.";
  third.querySelector('[name="role"]').value = "arbitrator"; third.querySelector('[name="role"]').listeners.change();
  assert(third.querySelector('[name="voice"]').parentLabel.hidden && third.querySelector('[name="eagerness"]').parentLabel.hidden && third.querySelector('[name="speaks_for"]').parentLabel.hidden);
  assert(!third.querySelector('[name="role_note"]').hidden); assert.match(third.querySelector('[name="role_note"]').textContent, /text only/);
  await assert.rejects(() => launched.submitSetup(), /only allowed in a negotiation/);                 // casual rooms have no arbitrator
  third.querySelector('[name="role"]').value = "voice"; third.querySelector('[name="role"]').listeners.change();
  launched.$("openai-key").value = "sk-synthetic-key-value-0000000000";
  await launched.submitSetup();
  const setup = runtimeCalls.find(c => c.url.endsWith("/setup"));
  assert.deepEqual(JSON.parse(setup.options.body), {conversation_type:"casual", participants:[{name:"Synthetic One",contact:"one@example.invalid"},{name:"Synthetic Two",contact:"555-0100"}],
    agents:[{name:"Ava", role:"voice", voice:"marin", eagerness:"balanced", speaks_for:"", instructions:"Answer in haiku."}, {name:"Ben", role:"voice", voice:"cedar", eagerness:"quiet", speaks_for:"Synthetic Two", instructions:"Only words starting with A."},
      {name:"Cy", role:"voice", voice:"sage", eagerness:"eager", speaks_for:"Synthetic One", instructions:"Speak only in questions."}], openai_api_key:"sk-synthetic-key-value-0000000000"});
  assert.equal(setup.options.headers.Authorization, "Bearer launcher_token");
  assert.equal(launched.$("openai-key").value, "");                       // key never lingers in the page
  assert.equal(launched.session, "room_auto");                            // runtime's room opened automatically
  assert(launched.$("setup-form").hidden); assert(launched.$("summary-panel").hidden);   // 404 summary on open: nothing to show
  phase = "enrollment"; awaiting = "participant_1"; await launched.pollRuntime();
  const recordButton = walk(launched.$("enrollment")).find(e => e.tagName === "button");
  assert.match(recordButton.textContent, /Record Synthetic One now/); assert(!recordButton.disabled); assert(launched.$("start").hidden);
  assert(!launched.$("gate").hidden); assert(!launched.$("enrollment-section").hidden); assert(launched.$("room-section").hidden);   // still gated during enrollment
  assert(launched.$("objectives-section").hidden);                                                                                  // casual room: no objectives step
  await launched.recordParticipant("participant_1");
  assert.deepEqual(JSON.parse(runtimeCalls.find(c => c.url.endsWith("/enrollment/record")).options.body), {participant_id:"participant_1"});
  assert.equal(launched.$("phase").textContent, "ready"); assert(!launched.$("start").hidden);
  assert(launched.$("gate").hidden); assert.equal(launched.$("mic").getAttribute("data-state"), "ready");                         // gate lifts once enrolled
  launched.gateForced = true; launched.renderStage(); assert(!launched.$("gate").hidden); assert(!launched.$("gate-close").hidden); // review is escapable
  launched.gateForced = false; launched.renderStage(); assert(launched.$("gate").hidden);
  assert.match(launched.$("enrollment").textContent, /peak 9000/);
  await launched.runtimeAction("/start"); assert.equal(launched.$("phase").textContent, "live"); assert(launched.$("start").hidden);
  assert.equal(launched.$("mic").getAttribute("data-state"), "on");
  // Live negotiation room: releases granted with negotiation_text, the runtime now reports the arbitrator.
  granted = true; convType = "negotiation";
  await launched.poll();
  assert.equal(launched.$("utterance-count").textContent, "4");
  const cards = launched.$("agents").children;
  assert.equal(cards.length, 3);
  assert.match(cards[0].className, /speaking/); assert.match(cards[1].className, /held/);
  assert.match(walk(cards[0]).find(e => e.className === "orb").style, /--h:\d+/);
  assert.notEqual(walk(cards[0]).find(e => e.className === "orb").style, walk(cards[1]).find(e => e.className === "orb").style);   // unique gradient per agent
  assert.equal(buttons(cards[0]).length, 3); assert(walk(cards[0]).some(e => e.tagName === "select"));
  // Arbitrator card: text only, Post now / Pause / Drop override, no eagerness select, arbitrator counters, orb present.
  assert.match(cards[2].className, /arbitrator/); assert.doesNotMatch(cards[2].className, /speaking/);
  assert.match(cards[2].textContent, /text only · gpt-5/); assert.match(cards[2].textContent, /Listening/); assert.match(cards[2].textContent, /Generations 3 · ingested rows 12 · pending tag none/);
  assert.deepEqual(buttons(cards[2]).map(b => b.title), ["Post now", "Pause", "Drop override"]); assert.deepEqual(buttons(cards[0]).map(b => b.title), ["Speak", "Hold", "Cancel"]);
  assert(!walk(cards[2]).some(e => e.tagName === "select")); assert(walk(cards[2]).some(e => e.className === "orb"));
  arbGenerations = 4; await launched.poll();
  assert.match(launched.$("agents").children[2].textContent, /Mediating/); assert.match(launched.$("agents").children[2].className, /speaking/);   // generation count rose
  // Raw channel: hidden until everyone reveals; the page shows no rows and keeps its cursor at 0 while hidden.
  const rawRequest = runtimeCalls.find(c => c.url.includes("/agent_channel?"));
  assert.equal(rawRequest.url, "/speaker/session/room_auto/agent_channel?after_id=0&limit=200&tier=raw");
  assert.match(launched.$("raw-state").textContent, /Hidden until every person in the room reveals it/);
  assert.equal(rows(launched.$("raw-channel")).length, 0); assert.equal(launched.rawCursor, 0);
  const revealButtons = buttons(launched.$("reveal-controls"));
  assert.deepEqual(revealButtons.map(b => b.textContent), ["Reveal to Synthetic One", "Reveal to Synthetic Two"]);
  await revealButtons[0].listeners.click();
  const reveal = runtimeCalls.filter(c => c.url.endsWith("/agent_channel/reveal")).at(-1);
  assert.equal(reveal.url, "/speaker/session/room_auto/agent_channel/reveal"); assert.equal(reveal.options.method, "POST");
  assert.deepEqual(JSON.parse(reveal.options.body), {participant_id:"participant_1", revealed:true});
  assert.equal(buttons(launched.$("reveal-controls"))[0].textContent, "Hide from Synthetic One");
  assert.equal(rows(launched.$("raw-channel")).length, 2); assert.equal(launched.rawCursor, 2); assert.match(launched.$("raw-state").textContent, /Revealed/);
  assert.match(launched.$("raw-channel").textContent, /OBJECTIVE_ACHIEVED/); assert.match(launched.$("raw-channel").textContent, /1 withheld/);
  const rawUrls = () => runtimeCalls.filter(c => c.url.includes("/agent_channel?")).map(c => c.url);
  assert.match(rawUrls().at(-1), /after_id=0&/);                                                        // the fetch that flipped to revealed started from 0
  await launched.poll(); assert.equal(rows(launched.$("raw-channel")).length, 2);                       // dedupe by row_id
  assert.match(rawUrls().at(-1), /after_id=2&/);                                                        // server cursor adopted while revealed
  revealed = false; await launched.poll();                                                              // someone withdrew: the API hides again
  assert.equal(rows(launched.$("raw-channel")).length, 0); assert.equal(launched.rawCursor, 0); assert.match(launched.$("raw-state").textContent, /Hidden until/);
  await launched.poll(); assert.match(rawUrls().at(-1), /after_id=0&/);                                 // hidden again: back to 0 so a later reveal refetches everything
  // Objectives: one block per person; saving posts only fields and masks the entry afterwards.
  launched.gateForced = true; launched.renderStage();
  assert(!launched.$("objectives-section").hidden); assert.equal(launched.$("objectives").children.length, 2);
  const block = launched.$("objectives").children[0]; assert.equal(block.tagName, "details"); assert(block.open);
  assert.match(block.textContent, /Hand the keyboard to Synthetic One/);
  const state = launched.objectiveBlocks.get("participant_1");
  state.position.value = "Sell the house";
  const constraint = launched.addConstraint("participant_1");
  constraint.querySelector('[name="label"]').value = "floor"; constraint.querySelector('[name="value"]').value = "300000";
  launched.addConstraint("participant_1");                                                              // an empty row is dropped
  await launched.saveObjective("participant_1");
  const objective = runtimeCalls.filter(c => c.url.endsWith("/objectives")).at(-1);
  assert.equal(objective.url, "/speaker/session/room_auto/objectives"); assert.equal(objective.options.headers.Authorization, "Bearer launcher_token");
  assert.deepEqual(JSON.parse(objective.options.body), {principal_id:"participant_1", position:"Sell the house", constraints:[{label:"floor", value:"300000"}], source:"typed", trigger:"initial"});
  assert(!block.open); assert.match(block.className, /recorded/); assert.equal(state.summary.textContent, "Synthetic One · Objective v1 recorded · 1 constraint");
  assert.equal(state.position.value, ""); assert.equal(state.rows.children.length, 0);
  assert(!walk(block).some(e => e.value === "300000" || e.value === "Sell the house")); assert.doesNotMatch(block.textContent, /300000|Sell the house/);
  launched.loadObjective("participant_2", parseObjectiveFile("Buy the house\nceiling: 320000\nmove in: August", "offer.txt"));
  assert.equal(launched.objectiveBlocks.get("participant_2").rows.children.length, 2);
  await launched.saveObjective("participant_2");
  assert.deepEqual(JSON.parse(runtimeCalls.at(-1).options.body), {principal_id:"participant_2", position:"Buy the house", constraints:[{label:"ceiling", value:"320000"}, {label:"move in", value:"August"}], source:"uploaded", trigger:"initial"});
  assert.match(launched.$("objectives").children[1].textContent, /Objective v1 recorded · 2 constraints/);
  state.position.value = "Sell the house, flexible on closing"; await launched.saveObjective("participant_1");
  assert.equal(JSON.parse(runtimeCalls.at(-1).options.body).trigger, "edited in console"); assert.match(block.textContent, /Objective v2 recorded · 0 constraints/);
  await launched.poll(); assert.equal(launched.$("objectives").children[0], block);                       // polling does not rebuild the blocks
  await assert.rejects(() => launched.saveObjective("participant_1"), /position first/);
  forbidObjectives = true; state.position.value = "Another version";
  await assert.rejects(() => launched.saveObjective("participant_1"), /403: negotiation_text scope missing/);
  assert.equal(state.status.textContent, "Every person must sign with the negotiation_text disclosure first.");
  forbidObjectives = false; launched.gateForced = false; launched.renderStage();
  // Summary: fetched once when the runtime phase turns ended, deletable.
  summary = {session_id:"room_auto", text:"Agreed: closing in August.\nOpen: price.", model:"gpt-5", board_rows:4, transcript_rows:61, created_ms:1780000100000, retention_deadline_ms:1780000100000 + 30 * 86400000};
  assert.match(launched.$("stop-runtime").textContent, /End conversation/);
  await launched.runtimeAction("/stop"); assert.equal(launched.$("phase").textContent, "ended"); assert(launched.$("stop-runtime").hidden);
  assert(!launched.$("summary-panel").hidden); assert.equal(launched.$("summary-text").textContent, "Agreed: closing in August.\nOpen: price.");
  assert.match(launched.$("summary-meta").textContent, /Model gpt-5 · saved .+ · kept until .+ · from 4 board rows and 61 transcript rows/);
  assert.equal(runtimeCalls.filter(c => c.url.endsWith("/summary") && c.options.method === "GET").length, 2);   // once on open, once on ended
  await launched.pollRuntime(); assert.equal(runtimeCalls.filter(c => c.url.endsWith("/summary") && c.options.method === "GET").length, 2);
  await launched.deleteSummary();
  const deletion = runtimeCalls.at(-1); assert.equal(deletion.url, "/speaker/session/room_auto/summary"); assert.equal(deletion.options.method, "DELETE"); assert.equal(deletion.options.body, undefined);
  assert(launched.$("summary-panel").hidden); assert.equal(launched.$("summary-text").textContent, "");

  // Negotiation setup: the page mirrors the runtime's roster rule before posting.
  let negoPhase = "setup"; const negoCalls = [];
  const nego = new Console(new Document(), async (url, options) => {
    negoCalls.push({url, options}); let data = {};
    if (url === "http://127.0.0.1:8090/bootstrap") data = {phase:negoPhase, session_id:null, gui:true, api_token:"launcher_token"};
    else if (url === "http://127.0.0.1:8090/agents") data = {session_id:null, agents:[], phase:negoPhase, needs_openai_key:false, conversation_type:"negotiation", voices:["marin","cedar"], eagerness_levels:["quiet","balanced","eager"], max_agents:4,
      agent_configs:[{name:"Ava", role:"voice", voice:"marin", eagerness:"balanced", instructions:"", speaks_for:"Synthetic One"}, {name:"Ben", role:"voice", voice:"cedar", eagerness:"balanced", instructions:"", speaks_for:"Synthetic Two"}], participants:[]};
    else if (url === "http://127.0.0.1:8090/setup") negoPhase = "consent";
    else if (url === "/privacy/notice") data = {configured:true,controller_name:"T",controller_address:"A",controller_email:"e@example.invalid",notice_text:"S",notice_sha256:"h",retention_text:"r",vendors:{openai_reviewed:true, cloudflare_reviewed:true},policy_version:"t",consent_method_version:"t"};
    else if (url === "/speaker/sessions?limit=100") data = {sessions:[]};
    else if (url === "/privacy/rooms") data = {rooms:[]};
    return {ok:true,status:200,json:async()=>data};
  }, {hostname:"127.0.0.1",protocol:"http:"});
  await nego.bootstrap(); await nego.pollRuntime();
  assert.equal(nego.$("conversation-type").value, "negotiation");
  nego.addSetupPerson(); nego.addSetupPerson();
  const [negoOne, negoTwo] = nego.$("setup-roster").children;
  negoOne.querySelector('[name="name"]').value = "Synthetic One"; negoOne.querySelector('[name="contact"]').value = "one@example.invalid";
  negoTwo.querySelector('[name="name"]').value = "Synthetic Two"; negoTwo.querySelector('[name="contact"]').value = "555-0100";
  await assert.rejects(() => nego.submitSetup(), /exactly one arbitrator/);
  nego.addAgentCard({name:"Mediator", role:"arbitrator", instructions:"Stay neutral."});
  const mediatorCard = nego.$("setup-agents").children[2];
  assert.equal(mediatorCard.querySelector('[name="role"]').value, "arbitrator"); assert(mediatorCard.querySelector('[name="voice"]').parentLabel.hidden); assert(!mediatorCard.querySelector('[name="role_note"]').hidden);
  nego.$("setup-agents").children[1].querySelector('[name="speaks_for"]').value = "Synthetic One";
  await assert.rejects(() => nego.submitSetup(), /each speaking for a different person/);
  nego.$("setup-agents").children[1].querySelector('[name="speaks_for"]').value = "Synthetic Two";
  nego.addAgentCard({name:"Dee", role:"voice", speaks_for:"Synthetic One"});
  await assert.rejects(() => nego.submitSetup(), /exactly two voice agents/);
  nego.$("setup-agents").replaceChildren(...nego.$("setup-agents").children.slice(0, 3));
  nego.$("conversation-type").value = "casual";
  await assert.rejects(() => nego.submitSetup(), /only allowed in a negotiation/);
  nego.$("conversation-type").value = "negotiation";
  await nego.submitSetup();
  assert.deepEqual(JSON.parse(negoCalls.find(c => c.url.endsWith("/setup")).options.body), {conversation_type:"negotiation",
    participants:[{name:"Synthetic One",contact:"one@example.invalid"},{name:"Synthetic Two",contact:"555-0100"}],
    agents:[{name:"Ava", role:"voice", voice:"marin", eagerness:"balanced", speaks_for:"Synthetic One", instructions:""}, {name:"Ben", role:"voice", voice:"cedar", eagerness:"balanced", speaks_for:"Synthetic Two", instructions:""},
      {name:"Mediator", role:"arbitrator", voice:"marin", eagerness:"quiet", speaks_for:"", instructions:"Stay neutral."}]});

  const manual = new Console(new Document(), async (url) => { if (url.endsWith("/bootstrap")) throw new Error("no runtime"); return {ok:true,status:200,json:async()=>({})}; }, {hostname:"127.0.0.1",protocol:"http:"});
  await manual.bootstrap(); assert.equal(manual.token, "");               // no launcher: paste the token as before
  console.log("DOM/mock API checks passed: 4 replay rows, 2 calls, 1 board row, dedupe, XSS text, effective gates, unchecked releases, negotiation_text scope, nonce binding, withdrawal clearing, objectives masked, raw channel gated by the API, arbitrator card, summary.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
