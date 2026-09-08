/* Injected by the opt-in browser test only. Every API response is synthetic. */
(function () {
  const nativeFetch = window.fetch.bind(window);
  let granted = false, ended = false;
  let fixture;
  const consent = () => ({session_id:"synthetic_room",policy_version:"test",state:ended ? "destroying" : granted ? "active" : "pending",allowed:granted && !ended,participants:[{id:"person_1",name:"Synthetic Person",bipa_consent_granted:granted && !ended,consent_timestamp:1780000000000}],scopes:{local_processing:granted,openai_audio:granted,hosted_mcp:granted}});
  window.fetch = async function (url, options = {}) {
    let data;
    if (String(url).startsWith("/ui/")) return nativeFetch(url,options);
    if (url === "/privacy/notice") data = {configured:true,controller_name:"Synthetic Controller",controller_address:"Test address only",controller_email:"test@example.invalid",notice_text:"SYNTHETIC TEST NOTICE. Speaker embeddings identify consenting speakers during this test room.",notice_sha256:"synthetic_hash",retention_text:"Destroy on session completion or withdrawal.",policy_version:"test",consent_method_version:"test",vendors:{openai_reviewed:true,cloudflare_reviewed:true}};
    else if (url === "/speaker/sessions?limit=100") data = {sessions:[]};
    else if (url === "/privacy/rooms") data = {rooms:[{session_id:"synthetic_room",state:"pending"}]};
    else if (url === "/privacy/profiles") data = {profiles:[]};   // v3.1: retained voiceprints list, empty in the smoke
    else if (url.endsWith("/consent")) data = consent();
    else if (url.endsWith("/challenge")) data = {challenge:"synthetic_nonce",notice_sha256:"synthetic_hash",expires_at_ms:Date.now()+100000};
    else if (url.endsWith("/consents/person_1")) { const body = JSON.parse(options.body); if (body.accepted !== true || body.signature_text !== "Synthetic Person" || body.challenge !== "synthetic_nonce") throw Error("Invalid written grant"); granted = true; data={consent_id:"test_receipt"}; }
    else if (url.includes("/events?")) { fixture ||= await (await nativeFetch("/ui/fixtures/replay.json")).json(); data=fixture; }
    else if (url.endsWith("/participants")) data = {participants:[{id:"person_1",name:"Synthetic Person",kind:"human"},{id:"agent_1",name:"Ava",kind:"agent"},{id:"agent_2",name:"Ben",kind:"agent"}]};
    else if (url.endsWith("/current")) data = {speaker_id:"person_1",status:"tentative",similarity:.72};
    else if (url.endsWith("/floor")) data = {held_by:"agent_1",expires_at_ms:Date.now()+15000,server_time_ms:Date.now()};
    else if (url.includes("/agent_channel?")) data = {session_id:"synthetic_room",next_after_id:0,revealed:false,rows:[],text:""};   // v3: raw tier stays hidden until everyone reveals
    else if (url.endsWith("/summary")) return new Response(JSON.stringify({error:"not_found",message:"No summary"}),{status:404,headers:{"Content-Type":"application/json"}});
    else if (url === "http://127.0.0.1:8090/agents") data={session_id:"synthetic_room",agents:[{name:"Ava",participant_id:"agent_1",provider:"openai_realtime",model:"synthetic_model",voice:"marin",eagerness:"balanced",held:false,responding:false},{name:"Ben",participant_id:"agent_2",provider:"openai_realtime",model:"synthetic_model",voice:"cedar",eagerness:"quiet",held:false,responding:false}]};
    else throw new Error(`Unmocked request blocked: ${url}`);
    return new Response(JSON.stringify(data),{status:200,headers:{"Content-Type":"application/json"}});
  };
  window.addEventListener("DOMContentLoaded", async function () {
    const result = document.createElement("output"); result.id="browser-smoke-result"; document.body.append(result);
    const until = async fn => { for(let i=0;i<80;i++) { if(fn()) return; await new Promise(resolve=>setTimeout(resolve,100)); } throw Error("Timed out waiting for UI"); };
    try {
      document.querySelector("#token").value="synthetic_test_token";
      document.querySelector("#connect-form").requestSubmit();
      await until(()=>document.querySelector("#connection").textContent === "Operator connected");
      document.querySelector("#room-id").value="synthetic_room";
      document.querySelector("#open-form").requestSubmit();
      await until(()=>document.querySelector("#consents form"));
      if (document.querySelectorAll("#consents input[type=checkbox]:checked").length) throw Error("Consent was prechecked");
      if (document.querySelectorAll("#transcript li:not(.empty)").length) throw Error("Transcript rendered before grant");
      const form=document.querySelector("#consents form");
      form.querySelector("input:not([type=checkbox])").value="Synthetic Person";
      for (const checkbox of form.querySelectorAll("input[type=checkbox]")) checkbox.click();
      form.requestSubmit();
      await until(()=>document.querySelectorAll("#transcript li:not(.empty)").length===4);
      if (document.querySelectorAll("#mcp-calls li:not(.empty)").length!==2) throw Error("Incorrect MCP row count");
      if (document.querySelector("#transcript img")) throw Error("Text interpreted as markup");
      if (document.querySelectorAll("#agents button:enabled").length!==6) throw Error("Wrong agent controls");
      if (document.querySelector("#token").value) throw Error("Token left in input");
      await new Promise(resolve=>setTimeout(resolve,1800));
      if (document.querySelectorAll("#transcript li:not(.empty)").length!==4) throw Error("Duplicate feed rows");
      result.textContent="PASS: written release, 4 transcript rows, 2 MCP calls, controls, dedupe, safe text";
    } catch(error) { result.textContent=`FAIL: ${error.message}`; }
  });
})();
