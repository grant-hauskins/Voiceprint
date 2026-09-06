package dev.voiceprint;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import java.net.*;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import static org.junit.jupiter.api.Assertions.*;

/** Shared-room contracts exercised without a worker, microphone, provider key or tunnel. */
class V2ContractTest {
    @TempDir Path temp;
    Store store;
    SpeakerService service;
    SpeakerServiceTest.MutableClock clock;
    final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();

    @BeforeEach void setup() throws Exception {
        store = new Store(temp.resolve("room.sqlite"));
        clock = new SpeakerServiceTest.MutableClock();
        service = new SpeakerService(store, new SpeakerServiceTest.FakeEngine(), clock, PrivacyTestSupport.POLICY);
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("room"));
    }
    @AfterEach void close() throws Exception { store.close(); client.close(); }
    static ObjectNode agent(String id, String name) {
        return Json.obj().put("id", id).put("name", name).put("kind", "agent").put("provider", "openai_realtime").put("model", "gpt-realtime-2.1");
    }
    static ObjectNode utterance(String who, long start, String text) {
        return Json.obj().put("speaker_id", who).put("start_ms", start).put("end_ms", start + 1000).put("text", text);
    }
    static ObjectNode claim(String who, long duration) { return Json.obj().put("participant_id", who).put("lease_ms", duration); }
    void agents() { service.register("room", agent("ava", "Ava")); service.register("room", agent("ben", "Ben")); }
    long count(String table) throws Exception {
        try (var p = store.prepare("SELECT count(*) FROM " + table); var r = p.executeQuery()) { return r.getLong(1); }
    }
    HttpResponse<String> request(String base, String method, String path, JsonNode body, String token, String origin) throws Exception {
        var builder = HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(3));
        if (token != null) builder.header("Authorization", "Bearer " + token);
        if (origin != null) builder.header("Origin", origin);
        if (body != null) builder.header("Content-Type", "application/json");
        builder.method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body.toString()));
        return client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    @Test void registrationIsIdempotentValidatesFieldsAndNeverEnrollsAgents() throws Exception {
        assertTrue(service.register("room", agent("ava", "Ava")).created());
        assertFalse(service.register("room", agent("ava", "Ava")).created());
        assertEquals(409, assertThrows(ApiException.class, () -> service.register("room", agent("ava", "Other"))).status);
        assertEquals(409, assertThrows(ApiException.class, () -> service.register("room", agent("a", "Alice"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.register("room", agent("ben", "Ben").put("kind", "human"))).status);
        for (String field : List.of("name", "provider", "model")) {
            assertEquals(400, assertThrows(ApiException.class, () -> service.register("room", agent("ben", "Ben").put(field, " "))).status);
            assertEquals(400, assertThrows(ApiException.class, () -> service.register("room", agent("ben", "Ben").put(field, "x".repeat(201)))).status);
        }
        assertEquals(403, assertThrows(ApiException.class, () -> service.register("missing", agent("ava", "Ava"))).status);
        var participants = service.participants("room").path("participants");
        assertEquals(List.of("a", "ava", "b"), new ArrayList<JsonNode>() {{ participants.forEach(this::add); }}.stream().map(p -> p.path("id").asText()).toList());
        assertEquals("human", participants.get(0).path("kind").asText());
        assertTrue(participants.get(0).path("provider").isNull());
        assertEquals("test-model", participants.get(0).path("model").asText());
        assertEquals(2, service.profiles("room").path("participants").size());
        assertEquals(2, count("profiles"));
        assertTrue(service.sessions(10).path("sessions").get(0).path("participants").asText().contains("ava=Ava"));
        for (int i = 0; i < 6; i++) service.ingest("room", Json.obj().put("sequence", i).put("audio_base64", SpeakerServiceTest.AUDIO));
        assertEquals(2, service.current("room").path("candidates").size());
        service.end("room");
        assertEquals(403, assertThrows(ApiException.class, () -> service.register("room", agent("ben", "Ben"))).status);
    }

    @Test void restRegistrationReturns201Then200() throws Exception {
        try (var api = new RestServer(service, 0, "test-secret")) {
            api.start(); String base = "http://127.0.0.1:" + api.port();
            assertEquals(201, request(base, "POST", "/speaker/session/room/participants", agent("ava", "Ava"), "test-secret", null).statusCode());
            var retry = request(base, "POST", "/speaker/session/room/participants", agent("ava", "Ava"), "test-secret", null);
            assertEquals(200, retry.statusCode());
            assertEquals(agent("ava", "Ava"), Json.parse(retry.body()).path("participant"));
            assertEquals(3, Json.parse(request(base, "GET", "/speaker/session/room/participants", null, "test-secret", null).body()).path("participants").size());
        }
    }

    @Test void agentUtterancesAreDeclaredAndPreservedByEveryLabelFilter() {
        agents();
        var posted = service.utter("room", utterance("ava", 1000, "Ben, your turn.").put("source", "agent").put("similarity", .99).put("margin", .8).put("overlap_ratio", 1));
        assertEquals("agent", posted.path("label").asText());
        service.utter("room", utterance("a", 3000, "A human line").put("similarity", .3).put("margin", .06));
        for (String label : List.of("high", "medium", "low")) {
            var response = service.utterances("room", 0, 100, label);
            var row = response.path("utterances").get(0);
            assertEquals("Ava", row.path("speaker_name").asText());
            assertTrue(row.path("similarity").isNull()); assertTrue(row.path("margin").isNull());
            assertTrue(row.path("overlap_ratio").isNull()); assertTrue(row.path("abstain_ratio").isNull());
            assertTrue(row.path("candidates").isEmpty());
            assertTrue(response.path("text").asText().contains("Ava [agent]: Ben, your turn."));
            assertEquals("similarity_based_uncalibrated", response.path("label_kind").asText());
        }
        assertEquals(400, assertThrows(ApiException.class, () -> service.utter("room", utterance("a", 0, "x").put("source", "agent"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.utter("room", utterance(null, 0, "x").put("source", "agent"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.utter("room", utterance("ava", 0, "x"))).status);
        assertEquals(404, assertThrows(ApiException.class, () -> service.utter("room", utterance("missing", 0, "x").put("source", "agent"))).status);
    }

    @Test void idPaginationDoesNotLoseLateArrivingEarlierAudio() {
        agents();
        service.utter("room", utterance("ava", 6000, "agent finishes first").put("source", "agent"));
        service.utter("room", utterance("a", 1000, "human transcription finishes later"));
        service.utter("room", utterance("ben", 9000, "third").put("source", "agent"));
        long cursor = 0;
        for (long expected = 1; expected <= 3; expected++) {
            var page = service.utterances("room", cursor, 1, null);
            assertEquals(expected, page.path("utterances").get(0).path("utterance_id").asLong());
            cursor = page.path("next_after_id").asLong();
        }
        assertEquals(3, cursor);
        assertTrue(service.utterances("room", cursor, 1, null).path("utterances").isEmpty());
    }

    @Test void floorContentionRenewalExpiryAndEndArePersisted() throws Exception {
        agents();
        var first = service.claimFloor("room", claim("ava", 1000));
        assertTrue(first.path("granted").asBoolean());
        assertEquals(clock.now + 1000, first.path("expires_at_ms").asLong());
        assertFalse(service.claimFloor("room", claim("ben", 1000)).path("granted").asBoolean());
        assertFalse(service.releaseFloor("room", "ben").path("released").asBoolean());
        clock.now += 500;
        assertEquals(clock.now + 1000, service.claimFloor("room", claim("ava", 1000)).path("expires_at_ms").asLong());
        store.close(); store = new Store(temp.resolve("room.sqlite"));
        service = new SpeakerService(store, new SpeakerServiceTest.FakeEngine(), clock, PrivacyTestSupport.POLICY);
        assertEquals("ava", service.floor("room").path("held_by").asText());
        clock.now += 1000;
        assertTrue(service.floor("room").path("held_by").isNull());
        assertTrue(service.floor("room").path("expires_at_ms").isNull());
        assertFalse(service.releaseFloor("room", "ava").path("released").asBoolean());
        assertTrue(service.claimFloor("room", claim("ben", 1000)).path("granted").asBoolean());
        var actions = new ArrayList<String>();
        for (var event : service.events("room", 0, 100, 0).path("events")) actions.add(event.path("data").path("action").asText());
        assertEquals(List.of("granted", "renewed", "expired", "granted"), actions);
        service.end("room");
        assertEquals(403, assertThrows(ApiException.class, () -> service.floor("room")).status);
        assertEquals(403, assertThrows(ApiException.class, () -> service.claimFloor("room", claim("ava", 1000))).status);
        service.sweepPrivacy(); assertEquals(0, count("floor"));
    }

    @Test void simultaneousFloorClaimsHaveExactlyOneWinner() throws Exception {
        agents();
        try (var pool = Executors.newFixedThreadPool(2)) {
            var start = new CountDownLatch(1);
            var a = pool.submit(() -> { start.await(); return service.claimFloor("room", claim("ava", 1000)); });
            var b = pool.submit(() -> { start.await(); return service.claimFloor("room", claim("ben", 1000)); });
            start.countDown();
            var ar = a.get(2, TimeUnit.SECONDS); var br = b.get(2, TimeUnit.SECONDS);
            assertNotEquals(ar.path("granted").asBoolean(), br.path("granted").asBoolean());
            assertEquals(ar.path("held_by"), br.path("held_by"));
            assertEquals(1, count("floor")); assertEquals(1, count("events"));
        }
    }

    @Test void floorInputAndPaginationAreBounded() {
        agents();
        assertEquals(400, assertThrows(ApiException.class, () -> service.claimFloor("room", claim("a", 1000))).status);
        assertEquals(404, assertThrows(ApiException.class, () -> service.claimFloor("room", claim("missing", 1000))).status);
        for (long duration : new long[]{999, 30001}) assertEquals(400, assertThrows(ApiException.class, () -> service.claimFloor("room", claim("ava", duration))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.claimFloor("room", claim("ava", 1000).put("lease_ms", 1000.5))).status);
        assertEquals(clock.now + 15000, service.claimFloor("room", Json.obj().put("participant_id", "ava")).path("expires_at_ms").asLong());
        assertEquals(400, assertThrows(ApiException.class, () -> service.events("room", -1, 1, 0)).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.events("room", 0, 201, 0)).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.events("room", 0, 1, 10001)).status);
    }

    @Test void eventFeedIsOrderedPagedAndIncludesCompleteRows() {
        agents();
        service.utter("room", utterance("a", 5000, "first stored"));
        service.claimFloor("room", claim("ava", 1000));
        service.utter("room", utterance("ava", 1000, "second stored").put("source", "agent"));
        service.releaseFloor("room", "ava");
        var page = service.events("room", 0, 2, 0);
        assertEquals(2, page.path("events").size());
        assertEquals("utterance", page.path("events").get(0).path("type").asText());
        assertEquals(service.utterances("room", 0, 1, null).path("utterances").get(0).toString(), page.path("events").get(0).path("data").toString());
        long cursor = page.path("next_after_id").asLong();
        var next = service.events("room", cursor, 2, 0);
        assertTrue(next.path("events").get(0).path("event_id").asLong() > cursor);
        cursor = next.path("next_after_id").asLong();
        assertEquals(cursor, service.events("room", cursor, 2, 0).path("next_after_id").asLong());
    }

    @Test void persistenceFailureRollsBackUtteranceAndFloorWithTheirEvents() throws Exception {
        agents();
        store.execute("CREATE TRIGGER reject_events BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'test storage failure'); END");
        assertThrows(IllegalStateException.class, () -> service.utter("room", utterance("a", 0, "must roll back")));
        assertEquals(0, count("utterances"));
        assertThrows(IllegalStateException.class, () -> service.claimFloor("room", claim("ava", 1000)));
        assertEquals(0, count("floor")); assertEquals(0, count("events"));
        assertThrows(IllegalStateException.class, () -> service.recordMcpCall("127.0.0.1", "ava", "get_transcript", Json.obj().put("session_id", "room"), 10, false));
        assertEquals(0, count("mcp_calls"));
    }

    @Test void longPollDoesNotStarveAudioAndWakesOnCommittedEvent() throws Exception {
        try (var api = new RestServer(service, 0, "test-secret")) {
            api.start(); String base = "http://127.0.0.1:" + api.port();
            var polls = new ArrayList<CompletableFuture<HttpResponse<String>>>();
            for (int i = 0; i < 8; i++) polls.add(client.sendAsync(HttpRequest.newBuilder(URI.create(base + "/speaker/session/room/events?wait_ms=5000")).header("Authorization", "Bearer test-secret").build(), HttpResponse.BodyHandlers.ofString()));
            Thread.sleep(150);
            assertTrue(polls.stream().noneMatch(CompletableFuture::isDone));
            var audio = request(base, "POST", "/speaker/session/room/audio", Json.obj().put("sequence", 0).put("audio_base64", SpeakerServiceTest.AUDIO), "test-secret", null);
            assertEquals(200, audio.statusCode());
            assertEquals("buffering", Json.parse(audio.body()).path("status").asText());
            service.utter("room", utterance("a", 0, "wake every poll"));
            for (var poll : polls) assertEquals("wake every poll", Json.parse(poll.get(2, TimeUnit.SECONDS).body()).path("events").get(0).path("data").path("text").asText());
            var empty = request(base, "GET", "/speaker/session/room/events?after_id=1&wait_ms=30", null, "test-secret", null);
            assertEquals(1, Json.parse(empty.body()).path("next_after_id").asLong());
            assertTrue(Json.parse(empty.body()).path("events").isEmpty());
        }
    }

    @Test void version3MigrationPreservesProfilesHistoryAndBackfillsExactlyOnce() throws Exception {
        for (int i = 0; i < 6; i++) service.ingest("room", Json.obj().put("sequence", i).put("audio_base64", SpeakerServiceTest.AUDIO));
        service.correct("room", Json.obj().put("segment_id", service.current("room").path("segment_id").asText()).put("actual_speaker", "b"));
        service.utter("room", utterance("a", 5000, "first stored"));
        service.utter("room", utterance("b", 1000, "second stored").put("similarity", .6).put("margin", .3));
        var beforeProfiles = store.profiles("room");
        var beforeTranscript = service.transcript("room", null, -1, 100);
        var beforeCorrections = service.corrections("room", 0, 100);
        var beforeUtterances = service.utterances("room", 0, 100, null);
        for (String table : List.of("destruction_items", "destruction_jobs", "consent_challenges", "consent_audit", "biometric_consents", "privacy_rooms", "floor", "events", "mcp_calls", "participants")) store.execute("DROP TABLE " + table);
        store.execute("PRAGMA user_version=3");
        store.close(); store = new Store(temp.resolve("room.sqlite")); service = new SpeakerService(store, new SpeakerServiceTest.FakeEngine(), clock, PrivacyTestSupport.POLICY);
        assertEquals(2, store.participants("room").size());
        assertEquals(beforeTranscript.path("transcript"), store.transcript("room", null, -1, 100));
        assertEquals(beforeCorrections.path("corrections"), store.corrections("room", 0, 100));
        assertEquals(beforeUtterances.path("utterances"), store.utterances("room", 0, 100, 0));
        assertEquals("legacy_blocked", service.consentStatus("room").path("state").asText());
        assertEquals(403, assertThrows(ApiException.class, () -> service.participants("room")).status);
        for (int i = 0; i < 2; i++) {
            assertArrayEquals(beforeProfiles.get(i).anchor(), store.profiles("room").get(i).anchor());
            assertArrayEquals(beforeProfiles.get(i).vector(), store.profiles("room").get(i).vector());
        }
        var events = store.events("room", 0, 100);
        assertEquals(2, events.size());
        assertEquals(1, events.get(0).path("data").path("utterance_id").asLong());
        assertEquals(2, events.get(1).path("data").path("utterance_id").asLong());
        store.close(); store = new Store(temp.resolve("room.sqlite"));
        assertEquals(2, count("events"));
        try (var p = store.prepare("PRAGMA user_version"); var r = p.executeQuery()) { assertEquals(5, r.getInt(1)); }
    }

    @Test void deletingSessionCascadesAllRoomDataAndEventIdsNeverRepeat() throws Exception {
        agents(); service.utter("room", utterance("a", 0, "x")); service.claimFloor("room", claim("ava", 1000));
        service.recordMcpCall("127.0.0.1", "ava", "get_transcript", Json.obj().put("session_id", "room"), 10, false);
        service.recordMcpCall("127.0.0.1", "ava", "get_transcript", Json.obj().put("session_id", "missing"), 10, true);
        long last = service.events("room", 0, 100, 0).path("next_after_id").asLong();
        service.delete("room");
        service.sweepPrivacy();
        for (String table : List.of("profiles", "participants", "utterances", "floor", "events")) assertEquals(0, count(table));
        assertEquals(1, count("mcp_calls"));
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("room2")); service.utter("room2", utterance("a", 0, "new session"));
        assertTrue(service.events("room2", 0, 100, 0).path("events").get(0).path("event_id").asLong() > last);
    }

    @Test void httpMcpCallsPersistServerProofIncludingFailuresAndExactUtf8Bytes() throws Exception {
        agents(); service.utter("room", utterance("a", 0, "Hello café 世界"));
        try (var api = new RestServer(service, 0, "api-test")) {
            api.start(); String base = "http://127.0.0.1:" + api.port();
            try (var mcp = new McpHttpServer(0, base, "api-test", "mcp-test", service)) {
                mcp.start(); String url = "http://127.0.0.1:" + mcp.port();
                ObjectNode message = Json.obj().put("jsonrpc", "2.0").put("id", 1).put("method", "tools/call");
                message.set("params", Json.obj().put("name", "get_transcript").set("arguments", Json.obj().put("session_id", "room")));
                var call = request(url, "POST", "/mcp?participant_id=ava", message, "mcp-test", null);
                assertEquals(200, call.statusCode());
                assertEquals("similarity_based_uncalibrated", Json.parse(call.body()).path("result").path("label_kind").asText());
                var proof = service.events("room", 1, 100, 0).path("events").get(0).path("data");
                assertEquals("ava", proof.path("participant_id").asText());
                assertEquals(call.body().getBytes(StandardCharsets.UTF_8).length, proof.path("bytes").asInt());
                assertEquals("get_transcript", proof.path("tool").asText());
                assertEquals("127.0.0.1", proof.path("caller_ip").asText());
                assertFalse(proof.path("failed").asBoolean());
                assertFalse(proof.toString().contains("api-test")); assertFalse(proof.toString().contains("mcp-test"));
                assertEquals(401, request(url, "POST", "/mcp?participant_id=ava", message, "wrong", null).statusCode());
                assertEquals(403, request(url, "POST", "/mcp", message, "mcp-test", base).statusCode());
                assertEquals(1, count("mcp_calls"));
                ((ObjectNode) message.path("params")).put("name", "unknown_tool");
                request(url, "POST", "/mcp?participant_id=ben", message, "mcp-test", null);
                var failed = service.events("room", 2, 100, 0).path("events").get(0).path("data");
                assertTrue(failed.path("failed").asBoolean()); assertEquals("ben", failed.path("participant_id").asText());
                ((ObjectNode) message.path("params")).put("name", "get_current_speaker");
                request(url, "POST", "/mcp?participant_id=a", message, "mcp-test", null);
                assertTrue(service.events("room", 3, 100, 0).path("events").get(0).path("data").path("participant_id").isNull());
                ((ObjectNode) message.path("params").path("arguments")).put("session_id", "missing");
                request(url, "POST", "/mcp?participant_id=ava", message, "mcp-test", null);
                assertEquals(4, count("mcp_calls")); assertEquals(4, count("events"));
                try (var p = store.prepare("SELECT session_id,participant_id,failed FROM mcp_calls ORDER BY id DESC LIMIT 1"); var r = p.executeQuery()) {
                    assertNull(r.getString(1)); assertNull(r.getString(2)); assertTrue(r.getBoolean(3));
                }
            }
        }
        store.close(); store = new Store(temp.resolve("room.sqlite"));
        assertEquals(4, count("mcp_calls")); assertEquals(4, count("events"));
    }

    @Test void uiIsLocalSameOriginStaticAndJsonStillRequiresToken() throws Exception {
        Path web = Files.createDirectories(temp.resolve("web"));
        Files.writeString(web.resolve("index.html"), "<h1>Room</h1>");
        Files.writeString(web.resolve("app.js"), "const room = true;");
        Files.createDirectories(web.resolve("folder"));
        Files.writeString(temp.resolve("secret.txt"), "outside");
        try (var api = new RestServer(service, 0, "api-test", web)) {
            api.start(); String base = "http://127.0.0.1:" + api.port();
            for (String path : List.of("/ui", "/ui/")) assertEquals("<h1>Room</h1>", request(base, "GET", path, null, null, base).body());
            var js = request(base, "GET", "/ui/app.js", null, null, null);
            assertEquals(200, js.statusCode()); assertTrue(js.headers().firstValue("Content-Type").orElseThrow().startsWith("text/javascript"));
            assertEquals(401, request(base, "GET", "/speaker/sessions", null, null, base).statusCode());
            assertEquals(200, request(base, "GET", "/speaker/sessions", null, "api-test", base).statusCode());
            assertEquals(403, request(base, "GET", "/ui", null, null, "http://localhost:" + api.port()).statusCode());
            assertEquals(403, request(base, "GET", "/ui", null, null, "https://example.com").statusCode());
            assertEquals(403, request(base, "GET", "/ui", null, null, "null").statusCode());
            assertEquals(404, request(base, "GET", "/ui/folder", null, null, null).statusCode());
            assertEquals(404, request(base, "GET", "/ui/missing.js", null, null, null).statusCode());
            for (String path : List.of("/ui/../secret.txt", "/ui/%2e%2e/secret.txt", "/ui/%252e%252e/secret.txt", "/ui/%2e%2e%5csecret.txt", "/ui/app.js:secret"))
                assertEquals(403, request(base, "GET", path, null, null, null).statusCode(), path);
            // Java HttpClient restricts Host overrides; a raw local request verifies DNS rebinding protection.
            try (var socket = new Socket("127.0.0.1", api.port())) {
                socket.setSoTimeout(2000);
                socket.getOutputStream().write("GET /ui HTTP/1.1\r\nHost: 127.0.0.1:1\r\nConnection: close\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
                assertTrue(new String(socket.getInputStream().readAllBytes(), StandardCharsets.UTF_8).startsWith("HTTP/1.1 403"));
            }
        }
    }

    @Test void uiRejectsSymlinkEscapes() throws Exception {
        Path web = Files.createDirectories(temp.resolve("linked-web"));
        Path outside = Files.writeString(temp.resolve("private.txt"), "private");
        try { Files.createSymbolicLink(web.resolve("escape.txt"), outside); }
        catch (UnsupportedOperationException | java.nio.file.FileSystemException e) { Assumptions.abort("OS does not allow test symlinks: " + e.getClass().getSimpleName()); }
        try (var api = new RestServer(service, 0, null, web)) {
            api.start(); assertEquals(403, request("http://127.0.0.1:" + api.port(), "GET", "/ui/escape.txt", null, null, null).statusCode());
        }
    }
}
