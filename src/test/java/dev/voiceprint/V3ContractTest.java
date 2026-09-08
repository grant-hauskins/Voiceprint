package dev.voiceprint;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import java.net.*;
import java.net.http.*;
import java.nio.file.*;
import java.time.*;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

/**
 * V3 arbitration contract (docs/API.md) exercised without a worker, provider key or tunnel. Test names cite the
 * BUILD_SPEC_V3.md section from HANDOFF_PROMPT.md §K that each one proves.
 */
class V3ContractTest {
    @TempDir Path temp;
    Store store;
    SpeakerService service;
    SpeakerServiceTest.MutableClock clock;
    final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    static final String TOKEN = "test-secret";

    @BeforeEach void setup() throws Exception {
        store = new Store(temp.resolve("room.sqlite"));
        clock = new SpeakerServiceTest.MutableClock();
        service = new SpeakerService(store, new SpeakerServiceTest.FakeEngine(), clock, PrivacyTestSupport.POLICY);
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("room"));
        service.register("room", V2ContractTest.agent("ava", "Ava")); service.register("room", V2ContractTest.agent("ben", "Ben"));
    }
    @AfterEach void close() throws Exception { store.close(); client.close(); }
    static ObjectNode objective(String principal, String... values) {
        var n = Json.obj().put("principal_id", principal).put("position", "wants to sell the property").put("source", "typed").put("trigger", "initial");
        var constraints = n.putArray("constraints");
        for (String value : values) constraints.add(Json.obj().put("label", "floor").put("value", value));
        return n;
    }
    static ObjectNode post(String sender, String tier, String text) { return Json.obj().put("sender_participant_id", sender).put("tier", tier).put("text", text); }
    static ObjectNode summary(String text) { return Json.obj().put("text", text).put("model", "gpt-5").put("board_rows", 4).put("transcript_rows", 61); }
    long count(String table) throws Exception {
        try (var p = store.prepare("SELECT count(*) FROM " + table); var r = p.executeQuery()) { return r.getLong(1); }
    }
    HttpResponse<String> request(String base, String method, String path, JsonNode body, Map<String, String> headers) throws Exception {
        var builder = HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(3));
        if (!headers.containsKey("Authorization")) builder.header("Authorization", "Bearer " + TOKEN);
        headers.forEach(builder::header);
        if (body != null) builder.header("Content-Type", "application/json");
        builder.method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body.toString()));
        return client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }
    static String clockText(long ms) {
        var t = LocalTime.ofInstant(Instant.ofEpochMilli(ms), ZoneId.systemDefault());
        return String.format("%02d:%02d:%02d", t.getHour(), t.getMinute(), t.getSecond());
    }

    @Test void spec2_objectivesAreVersionedPerPrincipalNeverOverwritten() throws Exception {
        assertEquals(1, service.createObjective("room", objective("a", "300000")).path("version").asLong());
        clock.now += 10;
        var second = service.createObjective("room", objective("a", "310000").put("trigger", "counter offer heard"));
        assertEquals(2, second.path("version").asLong()); assertEquals(clock.now, second.path("created_ms").asLong());
        assertEquals(1, service.createObjective("room", objective("b", "$275,000")).path("version").asLong());
        var latest = service.objectives("room", false).path("objectives");
        assertEquals(2, latest.size());
        assertEquals("a", latest.get(0).path("principal_id").asText()); assertEquals(2, latest.get(0).path("version").asLong());
        assertEquals("310000", latest.get(0).path("constraints").get(0).path("value").asText());
        assertEquals("counter offer heard", latest.get(0).path("trigger").asText());
        assertEquals("b", latest.get(1).path("principal_id").asText()); assertEquals(1, latest.get(1).path("version").asLong());
        var history = service.objectives("room", true).path("objectives");
        assertEquals(3, history.size());
        assertEquals(List.of("a1", "a2", "b1"), List.of(history.get(0), history.get(1), history.get(2)).stream().map(o -> o.path("principal_id").asText() + o.path("version").asLong()).toList());
        assertEquals(3, count("objectives"));
        // Roster and shape limits.
        assertEquals(404, assertThrows(ApiException.class, () -> service.createObjective("room", objective("zed", "1"))).status);
        assertEquals(404, assertThrows(ApiException.class, () -> service.createObjective("room", objective("ava", "1"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.createObjective("room", objective("a", "1").put("source", "pasted"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.createObjective("room", objective("a", Collections.nCopies(21, "1").toArray(String[]::new)))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.createObjective("room", objective("a", "x".repeat(201)))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.createObjective("room", objective("a", "1").put("position", "x".repeat(2001)))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.createObjective("room", objective("a", "1").put("trigger", " "))).status);
        // Without every human's negotiation_text release the endpoint is refused, room or not.
        PrivacyTestSupport.authorize(service, "v2room", SpeakerServiceTest.initRequest("v2room").path("participants"), PrivacyTestSupport.V2_SCOPES);
        assertTrue(service.consentStatus("v2room").path("scopes").path("hosted_mcp").asBoolean());
        assertFalse(service.consentStatus("v2room").path("scopes").path("negotiation_text").asBoolean());
        var refused = assertThrows(ApiException.class, () -> service.createObjective("v2room", objective("a", "1")));
        assertEquals(403, refused.status); assertEquals("prior_written_release_required", refused.code);
        assertEquals(403, assertThrows(ApiException.class, () -> service.objectives("v2room", false)).status);
        try (var api = new RestServer(service, 0, TOKEN)) {
            api.start(); String base = "http://127.0.0.1:" + api.port();
            var created = request(base, "POST", "/speaker/session/room/objectives", objective("b", "280000"), Map.of());
            assertEquals(201, created.statusCode()); assertEquals(2, Json.parse(created.body()).path("version").asLong());
            assertEquals(2, Json.parse(request(base, "GET", "/speaker/session/room/objectives", null, Map.of()).body()).path("objectives").size());
            assertEquals(4, Json.parse(request(base, "GET", "/speaker/session/room/objectives?history=1", null, Map.of()).body()).path("objectives").size());
            assertEquals(403, request(base, "POST", "/speaker/session/v2room/objectives", objective("a", "1"), Map.of()).statusCode());
        }
    }

    @Test void spec3_rawStreamIsWithheldByTheApiUntilEveryHumanRevealsAndAgentsAlwaysSeeIt() throws Exception {
        service.postChannel("room", post("ben", "board", "Agreed: closing date is flexible"));
        service.postChannel("room", post("ava", "raw", "my side has room below the ask"));
        service.postChannel("room", post("ben", "raw", "mine will not go above the ask"));
        service.postChannel("room", post("ben", "board", "Open: price").put("tag", "REFOCUS_NEEDED"));
        try (var api = new RestServer(service, 0, TOKEN)) {
            api.start(); String base = "http://127.0.0.1:" + api.port();
            var origin = Map.of("Origin", base); var hosted = Map.of("X-Voiceprint-Hosted-MCP", "true");
            var page = Json.parse(request(base, "GET", "/speaker/session/room/agent_channel?tier=all", null, Map.of()).body());
            assertEquals(2, page.path("rows").size()); assertFalse(page.path("revealed").asBoolean());
            assertEquals(4, page.path("next_after_id").asLong(), "cursor advances past omitted raw rows");
            for (var row : page.path("rows")) assertEquals("board", row.path("tier").asText());
            assertFalse(page.path("text").asText().contains("[raw]"));
            assertEquals("#1 " + clockText(clock.now) + " Ben [board]: Agreed: closing date is flexible\n#4 " + clockText(clock.now) + " Ben [board](REFOCUS_NEEDED): Open: price\n", page.path("text").asText());
            var raw = Json.parse(request(base, "GET", "/speaker/session/room/agent_channel?tier=raw", null, Map.of()).body());
            assertEquals(0, raw.path("rows").size()); assertEquals(3, raw.path("next_after_id").asLong(), "highest raw row examined, even though every one was withheld");
            // One human's reveal is not enough; the request must come from the local browser origin and a roster member.
            assertEquals(403, request(base, "POST", "/speaker/session/room/agent_channel/reveal", Json.obj().put("participant_id", "a").put("revealed", true), Map.of()).statusCode());
            assertEquals(404, request(base, "POST", "/speaker/session/room/agent_channel/reveal", Json.obj().put("participant_id", "ava").put("revealed", true), origin).statusCode());
            assertEquals(400, request(base, "POST", "/speaker/session/room/agent_channel/reveal", Json.obj().put("participant_id", "a"), origin).statusCode());
            var one = Json.parse(request(base, "POST", "/speaker/session/room/agent_channel/reveal", Json.obj().put("participant_id", "a").put("revealed", true), origin).body());
            assertFalse(one.path("revealed").asBoolean()); assertEquals(1, one.path("revealed_by").size());
            assertEquals(2, Json.parse(request(base, "GET", "/speaker/session/room/agent_channel?tier=all", null, Map.of()).body()).path("rows").size());
            var both = Json.parse(request(base, "POST", "/speaker/session/room/agent_channel/reveal", Json.obj().put("participant_id", "b").put("revealed", true), origin).body());
            assertTrue(both.path("revealed").asBoolean()); assertEquals(List.of("a", "b"), List.of(both.path("revealed_by").get(0).asText(), both.path("revealed_by").get(1).asText()));
            var shown = Json.parse(request(base, "GET", "/speaker/session/room/agent_channel?tier=all", null, Map.of()).body());
            assertEquals(4, shown.path("rows").size()); assertTrue(shown.path("revealed").asBoolean()); assertTrue(shown.path("text").asText().contains("Ava [raw]: my side"));
            // Withdrawing either reveal hides the raw stream again.
            var withdrawn = Json.parse(request(base, "POST", "/speaker/session/room/agent_channel/reveal", Json.obj().put("participant_id", "a").put("revealed", false), origin).body());
            assertFalse(withdrawn.path("revealed").asBoolean()); assertEquals(1, withdrawn.path("revealed_by").size());
            assertEquals(2, Json.parse(request(base, "GET", "/speaker/session/room/agent_channel?tier=all", null, Map.of()).body()).path("rows").size());
            // The agents' own path (hosted MCP header, separate credential) always receives both tiers and still reports the truthful flag.
            var agents = Json.parse(request(base, "GET", "/speaker/session/room/agent_channel?tier=all", null, hosted).body());
            assertEquals(4, agents.path("rows").size()); assertFalse(agents.path("revealed").asBoolean());
            assertEquals(2, Json.parse(request(base, "GET", "/speaker/session/room/agent_channel?tier=raw", null, hosted).body()).path("rows").size());
            assertEquals(400, request(base, "GET", "/speaker/session/room/agent_channel?tier=secret", null, Map.of()).statusCode());
        }
    }

    @Test void spec31_channelCursorNeverRewindsAndReturnsOnlyNewRows() {
        service.postChannel("room", post("ava", "board", "one"));
        service.postChannel("room", post("ben", "raw", "two"));
        service.postChannel("room", post("ava", "board", "three"));
        long cursor = 0;
        for (long expected = 1; expected <= 3; expected++) {
            var page = service.channel("room", cursor, 1, "all", true);
            assertEquals(1, page.path("rows").size());
            assertEquals(expected, page.path("rows").get(0).path("row_id").asLong());
            assertTrue(page.path("next_after_id").asLong() > cursor);
            cursor = page.path("next_after_id").asLong();
        }
        assertEquals(3, cursor);
        var empty = service.channel("room", cursor, 100, "all", true);
        assertTrue(empty.path("rows").isEmpty()); assertEquals(cursor, empty.path("next_after_id").asLong());
        // The human path skips the raw row but its cursor moves past it, so the row is never re-served later.
        var human = service.channel("room", 1, 1, "all", false);
        assertTrue(human.path("rows").isEmpty()); assertEquals(2, human.path("next_after_id").asLong());
        assertEquals(3, service.channel("room", 2, 1, "all", false).path("rows").get(0).path("row_id").asLong());
        assertEquals(400, assertThrows(ApiException.class, () -> service.channel("room", 0, 201, "all", true)).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.channel("room", -2, 1, "all", true)).status);
    }

    @Test void spec4_guardRunsOnTheWritePathAgainstEveryLatestObjectiveBeforeCommit() throws Exception {
        service.createObjective("room", objective("a", "300000", "June 30"));
        service.createObjective("room", objective("b", "$275,000"));
        long eventsBefore = count("events");
        var posted = service.postChannel("room", post("ava", "board", "Buyer can reach $300,000 by June 30; seller wants 275000 or two hundred seventy-five thousand"));
        assertEquals("Buyer can reach [withheld] by [withheld]; seller wants [withheld] or [withheld]", posted.path("text").asText());
        assertEquals(4, posted.path("redactions").asInt()); assertEquals("board", posted.path("tier").asText());
        var stored = service.channel("room", 0, 100, "all", true).path("rows").get(0);
        assertEquals(posted.path("text").asText(), stored.path("text").asText()); assertEquals(4, stored.path("redactions").asInt());
        assertEquals("Ava", stored.path("sender_name").asText());
        try (var p = store.prepare("SELECT text FROM agent_channel"); var r = p.executeQuery()) { r.next(); assertFalse(r.getString(1).contains("300")); }
        // A board row commits its event atomically with the redacted text; raw rows produce no event.
        var events = service.events("room", 0, 200, 0).path("events");
        assertEquals(eventsBefore + 1, events.size());
        var event = events.get(events.size() - 1);
        assertEquals("agent_channel", event.path("type").asText()); assertEquals(stored.toString(), event.path("data").toString());
        service.postChannel("room", post("ben", "raw", "private note with 300000 inside"));
        assertEquals(eventsBefore + 1, count("events"));
        assertEquals("private note with [withheld] inside", service.channel("room", 1, 100, "raw", true).path("rows").get(0).path("text").asText());
        // Only the latest version per principal is watched.
        service.createObjective("room", objective("a", "310000"));
        var updated = service.postChannel("room", post("ava", "raw", "300000 now, 310000 later"));
        assertEquals("300000 now, [withheld] later", updated.path("text").asText()); assertEquals(1, updated.path("redactions").asInt());
        var clean = service.postChannel("room", post("ava", "raw", "no figures here"));
        assertEquals(0, clean.path("redactions").asInt());
    }

    @Test void spec4_failedEventPersistenceRollsBackTheChannelRow() throws Exception {
        service.createObjective("room", objective("a", "300000"));
        store.execute("CREATE TRIGGER reject_events BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'test storage failure'); END");
        assertThrows(IllegalStateException.class, () -> service.postChannel("room", post("ava", "board", "300000 is the floor")));
        assertEquals(0, count("agent_channel")); assertEquals(0, count("events"));
        // Raw rows do not touch events, so they still commit.
        assertEquals(1, service.postChannel("room", post("ava", "raw", "300000 is the floor")).path("row_id").asLong());
        assertEquals(1, count("agent_channel"));
    }

    @Test void spec4_senderMustBeARegisteredAgentInAnActiveSession() throws Exception {
        assertEquals(400, assertThrows(ApiException.class, () -> service.postChannel("room", post("a", "board", "human"))).status);
        assertEquals(404, assertThrows(ApiException.class, () -> service.postChannel("room", post("nobody", "board", "ghost"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.postChannel("room", post("ava", "secret", "x"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.postChannel("room", Json.obj().put("sender_participant_id", "ava").put("text", "x"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.postChannel("room", post("ava", "raw", "x").put("tag", "URGENT"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.postChannel("room", post("ava", "raw", "x".repeat(4001)))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.postChannel("room", post("ava", "raw", " "))).status);
        var tagged = service.postChannel("room", post("ava", "board", "Deal reached").put("tag", "OBJECTIVE_ACHIEVED"));
        assertEquals("OBJECTIVE_ACHIEVED", service.channel("room", 0, 10, "all", false).path("rows").get(0).path("tag").asText());
        assertTrue(service.channel("room", 0, 10, "all", false).path("text").asText().contains("Ava [board](OBJECTIVE_ACHIEVED): Deal reached"));
        assertEquals(1, tagged.path("row_id").asLong());
        store.execute("UPDATE sessions SET status='ended' WHERE id='room'");
        assertEquals(409, assertThrows(ApiException.class, () -> service.postChannel("room", post("ava", "raw", "late"))).status);
        store.execute("UPDATE sessions SET status='active' WHERE id='room'");
        service.end("room");
        assertEquals(403, assertThrows(ApiException.class, () -> service.postChannel("room", post("ava", "raw", "after end"))).status);
        assertEquals(403, assertThrows(ApiException.class, () -> service.channel("room", 0, 10, "all", true)).status);
    }

    @Test void spec31_mcpChannelToolsAttributeTheUrlParticipantAndRedactThroughTheSameGuard() throws Exception {
        service.createObjective("room", objective("a", "300000"));
        try (var api = new RestServer(service, 0, TOKEN)) {
            api.start(); String base = "http://127.0.0.1:" + api.port();
            try (var mcp = new McpHttpServer(0, base, TOKEN, "mcp-test", service)) {
                mcp.start(); String url = "http://127.0.0.1:" + mcp.port();
                var auth = Map.of("Authorization", "Bearer mcp-test");
                var list = Json.parse(request(url, "POST", "/mcp", Json.obj().put("jsonrpc", "2.0").put("id", 0).put("method", "tools/list"), auth).body());
                var names = new ArrayList<String>(); JsonNode postTool = null;
                for (var tool : list.path("result").path("tools")) { names.add(tool.path("name").asText()); if (tool.path("name").asText().equals("post_agent_channel")) postTool = tool; }
                assertTrue(names.containsAll(List.of("get_agent_channel", "post_agent_channel")));
                assertFalse(postTool.path("annotations").path("readOnlyHint").asBoolean());
                assertEquals(4000, postTool.path("inputSchema").path("properties").path("text").path("maxLength").asInt());
                ObjectNode read = Json.obj().put("jsonrpc", "2.0").put("id", 1).put("method", "tools/call");
                read.set("params", Json.obj().put("name", "get_agent_channel").set("arguments", Json.obj().put("session_id", "room")));
                var empty = Json.parse(request(url, "POST", "/mcp?participant_id=ava", read, auth).body()).path("result");
                assertFalse(empty.path("isError").asBoolean());
                assertEquals("(no channel messages yet)", empty.path("structuredContent").path("channel").asText());
                assertEquals(0, empty.path("structuredContent").path("count").asInt()); assertEquals(0, empty.path("structuredContent").path("next_after_id").asLong());
                assertEquals("(no channel messages yet)", empty.path("content").get(0).path("text").asText());
                try (var p = store.prepare("SELECT participant_id,tool FROM mcp_calls ORDER BY id"); var r = p.executeQuery()) { r.next(); assertEquals("ava", r.getString(1)); assertEquals("get_agent_channel", r.getString(2)); }
                // Posting stores the row as the URL participant, redacted by the server before commit.
                ObjectNode write = Json.obj().put("jsonrpc", "2.0").put("id", 2).put("method", "tools/call");
                write.set("params", Json.obj().put("name", "post_agent_channel").set("arguments", Json.obj().put("session_id", "room").put("tier", "board").put("text", "Ava here: the floor is 300000")));
                var posted = Json.parse(request(url, "POST", "/mcp?participant_id=ava", write, auth).body()).path("result");
                assertFalse(posted.path("isError").asBoolean());
                assertEquals(1, posted.path("structuredContent").path("row_id").asLong()); assertEquals("board", posted.path("structuredContent").path("tier").asText());
                assertEquals(1, posted.path("structuredContent").path("redactions").asInt());
                try (var p = store.prepare("SELECT sender_participant_id,tier,text,redactions FROM agent_channel"); var r = p.executeQuery()) {
                    r.next(); assertEquals("ava", r.getString(1)); assertEquals("board", r.getString(2)); assertEquals("Ava here: the floor is [withheld]", r.getString(3)); assertEquals(1, r.getInt(4));
                }
                // Without the URL tag the tool refuses before touching the API, and the argument text never reaches the proof log.
                ((ObjectNode) write.path("params").path("arguments")).put("text", "unattributed 300000");
                var refused = Json.parse(request(url, "POST", "/mcp", write, auth).body()).path("result");
                assertTrue(refused.path("isError").asBoolean()); assertTrue(refused.toString().contains("participant_id"));
                assertEquals(1, count("agent_channel"));
                assertEquals(3, count("mcp_calls"));
                try (var p = store.prepare("SELECT arguments,participant_id,failed FROM mcp_calls ORDER BY id"); var r = p.executeQuery()) {
                    while (r.next()) { assertFalse(r.getString(1).contains("300000")); assertFalse(r.getString(1).contains("Ava here")); }
                }
                for (var event : service.events("room", 0, 200, 0).path("events")) if (event.path("type").asText().equals("mcp_call")) assertFalse(event.toString().contains("300000"));
                // Default tier is raw and the hosted read path returns both tiers.
                ((ObjectNode) write.path("params").path("arguments")).remove("tier"); ((ObjectNode) write.path("params").path("arguments")).put("text", "private raw note");
                assertEquals("raw", Json.parse(request(url, "POST", "/mcp?participant_id=ben", write, auth).body()).path("result").path("structuredContent").path("tier").asText());
                var page = Json.parse(request(url, "POST", "/mcp?participant_id=ava", read, auth).body()).path("result").path("structuredContent");
                assertEquals(2, page.path("count").asInt()); assertEquals(2, page.path("next_after_id").asLong());
                assertTrue(page.path("channel").asText().contains("Ava [board]: Ava here: the floor is [withheld]"));
                assertTrue(page.path("channel").asText().contains("Ben [raw]: private raw note"));
                // A human tag is not an agent: the post is refused by the API and recorded unattributed.
                var human = Json.parse(request(url, "POST", "/mcp?participant_id=a", write, auth).body()).path("result");
                assertTrue(human.path("isError").asBoolean()); assertEquals(2, count("agent_channel"));
            }
        }
    }

    @Test void spec6_summaryIsRetainedAfterRoomDestructionAndSweptAtItsOwnDeadline() throws Exception {
        service.createObjective("room", objective("a", "300000"));
        service.postChannel("room", post("ava", "board", "Agreed: price"));
        service.reveal("room", Json.obj().put("participant_id", "a").put("revealed", true));
        var first = service.saveSummary("room", summary("Agreements: price. Open: date."));
        assertEquals(clock.now, first.path("created_ms").asLong());
        assertEquals(clock.now + PrivacyPolicy.SUMMARY_RETENTION_MS, first.path("retention_deadline_ms").asLong());
        clock.now += 1000;
        service.saveSummary("room", summary("Agreements: price and date."));
        assertEquals(1, count("summaries"));
        assertEquals("Agreements: price and date.", service.summary("room").path("text").asText());
        assertEquals(400, assertThrows(ApiException.class, () -> service.saveSummary("room", summary("x").put("board_rows", -1))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.saveSummary("room", summary("x".repeat(20001)))).status);
        service.end("room"); service.sweepPrivacy();
        assertTrue(service.destructionStatus("room").path("verified").asBoolean());
        for (String table : List.of("sessions", "objectives", "agent_channel", "channel_reveals", "participants", "events")) assertEquals(0, count(table), table);
        var retained = service.summary("room");
        assertEquals("Agreements: price and date.", retained.path("text").asText()); assertEquals("gpt-5", retained.path("model").asText());
        assertEquals(4, retained.path("board_rows").asLong()); assertEquals(61, retained.path("transcript_rows").asLong());
        assertEquals(403, assertThrows(ApiException.class, () -> service.saveSummary("room", summary("after end"))).status);
        try (var api = new RestServer(service, 0, TOKEN)) {
            api.start(); String base = "http://127.0.0.1:" + api.port();
            assertEquals(403, request(base, "POST", "/speaker/session/room/summary", summary("after end"), Map.of()).statusCode());
            var read = request(base, "GET", "/speaker/session/room/summary", null, Map.of());
            assertEquals(200, read.statusCode()); assertEquals("Agreements: price and date.", Json.parse(read.body()).path("text").asText());
            assertEquals(200, request(base, "DELETE", "/speaker/session/room/summary", null, Map.of()).statusCode());
            assertEquals(404, request(base, "GET", "/speaker/session/room/summary", null, Map.of()).statusCode());
            assertEquals(404, request(base, "DELETE", "/speaker/session/room/summary", null, Map.of()).statusCode());
            // A live room writes through REST with 201 and the deadline in the body.
            PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("room2"));
            var created = request(base, "POST", "/speaker/session/room2/summary", summary("second room"), Map.of());
            assertEquals(201, created.statusCode()); assertEquals(clock.now + PrivacyPolicy.SUMMARY_RETENTION_MS, Json.parse(created.body()).path("retention_deadline_ms").asLong());
        }
        assertEquals(1, count("summaries"));
        clock.now += PrivacyPolicy.SUMMARY_RETENTION_MS - 1; service.sweepPrivacy();
        assertEquals(1, count("summaries"));
        clock.now += 1; service.sweepPrivacy();
        assertEquals(0, count("summaries"));
        assertEquals(404, assertThrows(ApiException.class, () -> service.summary("room2")).status);
    }

    @Test void spec6_withdrawalOrDeletionRequestDestroysTheSummaryButPurposeCompletionRetainsIt() throws Exception {
        service.saveSummary("room", summary("withdrawn room"));
        assertEquals(1, count("summaries"));
        service.consentRevoke("room", "a");
        assertEquals(0, count("summaries"), "gone in the same transaction that schedules destruction");
        service.sweepPrivacy();
        assertTrue(service.destructionStatus("room").path("verified").asBoolean());
        assertEquals(404, assertThrows(ApiException.class, () -> service.summary("room")).status);
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("deleted"));
        service.saveSummary("deleted", summary("deleted room"));
        service.delete("deleted"); service.sweepPrivacy();
        assertEquals(404, assertThrows(ApiException.class, () -> service.summary("deleted")).status);
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("finished"));
        service.saveSummary("finished", summary("finished room"));
        service.end("finished"); service.sweepPrivacy();
        assertEquals("finished room", service.summary("finished").path("text").asText());
        assertEquals(1, count("summaries"));
    }

    @Test void spec2_negotiationTextScopeNeedsEveryHumanAndReviewedOpenAI() {
        assertTrue(service.consentStatus("room").path("scopes").path("negotiation_text").asBoolean());
        assertTrue(service.privacyNotice().path("notice_text").asText().contains("negotiation_text"));
        assertTrue(service.privacyNotice().path("notice_text").asText().contains("30 days"));
        var roster = SpeakerServiceTest.initRequest("half").path("participants");
        var room = Json.obj().put("session_id", "half").put("purpose_id", PrivacyPolicy.PURPOSE);
        for (var p : roster) room.withArray("participants").add(Json.obj().put("id", p.path("id").asText()).put("name", p.path("name").asText()).put("contact", "x@example.invalid"));
        service.createPrivacyRoom(room);
        PrivacyTestSupport.sign(service, "half", "a", "Alice", PrivacyTestSupport.ALL_SCOPES);
        PrivacyTestSupport.sign(service, "half", "b", "Bob", PrivacyTestSupport.V2_SCOPES);
        var scopes = service.consentStatus("half").path("scopes");
        assertTrue(scopes.path("local_processing").asBoolean()); assertTrue(scopes.path("hosted_mcp").asBoolean()); assertFalse(scopes.path("negotiation_text").asBoolean());
        assertEquals(403, assertThrows(ApiException.class, () -> service.requireConsent("half", "negotiation_text")).status);
        // The reveal gate is a v2-scope feature of a valid room; it does not depend on negotiation_text.
        assertFalse(service.reveal("half", Json.obj().put("participant_id", "a").put("revealed", true)).path("revealed").asBoolean());
    }

    @Test void spec2_unknownScopesAreRefusedAndUnreviewedOpenAIDisablesNegotiationText() throws Exception {
        var roster = SpeakerServiceTest.initRequest("strict").path("participants");
        var room = Json.obj().put("session_id", "strict").put("purpose_id", PrivacyPolicy.PURPOSE);
        for (var p : roster) room.withArray("participants").add(Json.obj().put("id", p.path("id").asText()).put("name", p.path("name").asText()).put("contact", "x@example.invalid"));
        service.createPrivacyRoom(room);
        assertEquals(403, assertThrows(ApiException.class, () -> PrivacyTestSupport.sign(service, "strict", "a", "Alice", List.of("negotiation_text", "everything"))).status);
        var unreviewed = new PrivacyPolicy("Synthetic Test Operator", "1 Test Street", "operator@example.invalid", TOKEN, false, true);
        var strict = new SpeakerService(store, new SpeakerServiceTest.FakeEngine(), clock, unreviewed);
        assertTrue(strict.consentStatus("room").path("scopes").path("local_processing").asBoolean());
        assertFalse(strict.consentStatus("room").path("scopes").path("negotiation_text").asBoolean());
        assertEquals(403, assertThrows(ApiException.class, () -> strict.createObjective("room", objective("a", "1"))).status);
    }

    @Test void schemaV6MigratesFromV5ExactlyOnceAndRefusesNewerFiles() throws Exception {
        service.createObjective("room", objective("a", "300000"));
        for (String table : List.of("summaries", "channel_reveals", "agent_channel", "objectives")) store.execute("DROP TABLE " + table);
        store.execute("PRAGMA user_version=5");
        store.close(); store = new Store(temp.resolve("room.sqlite"));
        try (var p = store.prepare("PRAGMA user_version"); var r = p.executeQuery()) { assertEquals(6, r.getInt(1)); }
        for (String table : List.of("summaries", "channel_reveals", "agent_channel", "objectives")) assertEquals(0, count(table));
        service = new SpeakerService(store, new SpeakerServiceTest.FakeEngine(), clock, PrivacyTestSupport.POLICY);
        assertEquals(1, service.createObjective("room", objective("a", "300000")).path("version").asLong());
        store.execute("PRAGMA user_version=7");
        store.close();
        assertThrows(IllegalStateException.class, () -> new Store(temp.resolve("room.sqlite")));
        store = new Store(temp.resolve("fresh.sqlite"));
    }
}
