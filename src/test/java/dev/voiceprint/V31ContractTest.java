package dev.voiceprint;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import java.net.*;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.*;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

/**
 * V3.1 contract (docs/API.md): transcript review feeding segment correction, per-person retained voiceprints,
 * provider-neutral notice. Synthetic PCM and a deterministic fake engine; no worker, provider key or tunnel.
 */
class V31ContractTest {
    @TempDir Path temp;
    Store store;
    SpeakerService service;
    SpeakerServiceTest.MutableClock clock;
    SpeakerServiceTest.FakeEngine engine;
    final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    static final String TOKEN = "test-secret";
    static final String KEY_A = PrivacyGate.subjectKey("Alice", "a@example.invalid"), KEY_B = PrivacyGate.subjectKey("Bob", "b@example.invalid");

    @BeforeEach void setup() throws Exception {
        store = new Store(temp.resolve("room.sqlite"));
        clock = new SpeakerServiceTest.MutableClock(); engine = new SpeakerServiceTest.FakeEngine();
        service = new SpeakerService(store, engine, clock, PrivacyTestSupport.POLICY);
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("room"));
        service.register("room", V2ContractTest.agent("ava", "Ava"));
    }
    @AfterEach void close() throws Exception { store.close(); client.close(); }
    long count(String table) throws Exception {
        try (var p = store.prepare("SELECT count(*) FROM " + table); var r = p.executeQuery()) { return r.getLong(1); }
    }
    void ingest(String session, int chunks) { for (int i = 0; i < chunks; i++) { service.ingest(session, Json.obj().put("sequence", i).put("audio_base64", SpeakerServiceTest.AUDIO)); clock.now += 250; } }
    static ObjectNode human(String who, long start, long end, String text, double similarity, double margin) {
        return Json.obj().put("speaker_id", who).put("start_ms", start).put("end_ms", end).put("text", text).put("similarity", similarity).put("margin", margin).put("overlap_ratio", 0).put("abstain_ratio", 0);
    }
    static ObjectNode review(String text, String speaker) { var n = Json.obj(); if (text != null) n.put("text", text); if (speaker != null) n.put("speaker_id", speaker); return n; }
    static double[] blend(double[] a, double[] b) { double[] m = new double[a.length]; for (int i = 0; i < m.length; i++) m[i] = .5 * a[i] + .5 * b[i]; return Audio.normalize(m); }
    JsonNode lastEvent(String session) { var events = service.events(session, 0, 200, 0).path("events"); return events.get(events.size() - 1); }
    HttpResponse<String> request(String base, String method, String path, JsonNode body, Map<String, String> headers) throws Exception {
        var builder = HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(3));
        if (!headers.containsKey("Authorization")) builder.header("Authorization", "Bearer " + TOKEN);
        headers.forEach(builder::header);
        if (body != null) builder.header("Content-Type", "application/json");
        builder.method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body.toString()));
        return client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    @Test void review_textOnlyKeepsTheAcousticLabelAndShowsTheOriginal() throws Exception {
        service.utter("room", human("a", 0, 2000, "we said Brian", .7, .4));
        long before = count("events");
        clock.now += 5;
        var reviewed = service.reviewUtterance("room", 1, review("we said Ryan", null));
        assertEquals(1, reviewed.path("utterance_id").asLong()); assertEquals("we said Ryan", reviewed.path("text").asText());
        assertEquals("a", reviewed.path("speaker_id").asText()); assertEquals("high", reviewed.path("label").asText(), "a text-only review keeps the acoustic label");
        assertEquals("we said Brian", reviewed.path("original_text").asText()); assertTrue(reviewed.path("original_speaker_id").isNull());
        assertEquals(clock.now, reviewed.path("reviewed_ms").asLong());
        assertEquals(0, reviewed.path("segments_corrected").asInt()); assertFalse(reviewed.path("profile_updated").asBoolean());
        var page = service.utterances("room", 0, 100, null);
        var row = page.path("utterances").get(0);
        assertEquals("we said Ryan", row.path("text").asText()); assertEquals("we said Brian", row.path("original_text").asText());
        assertEquals("high", row.path("label").asText()); assertEquals("similarity_based_uncalibrated", page.path("label_kind").asText());
        assertEquals("#1 0:00.0-0:02.0 Alice [high]: we said Ryan\n", page.path("text").asText());
        try (var p = store.prepare("SELECT text,reviewed_text FROM utterances"); var r = p.executeQuery()) { r.next(); assertEquals("we said Brian", r.getString(1)); assertEquals("we said Ryan", r.getString(2)); }
        // The event carries the full updated row, atomically with the review.
        assertEquals(before + 1, count("events"));
        var event = lastEvent("room");
        assertEquals("utterance_reviewed", event.path("type").asText()); assertEquals(row.toString(), event.path("data").toString());
        // Validation.
        assertEquals(400, assertThrows(ApiException.class, () -> service.reviewUtterance("room", 1, Json.obj())).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.reviewUtterance("room", 1, review("x".repeat(4001), null))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.reviewUtterance("room", 1, review(" ", null))).status);
        assertEquals(404, assertThrows(ApiException.class, () -> service.reviewUtterance("room", 99, review("x", null))).status);
        assertEquals(404, assertThrows(ApiException.class, () -> service.reviewUtterance("room", 1, review(null, "zed"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.reviewUtterance("room", 1, review(null, "ava"))).status, "an agent is not an enrolled human");
        // Agent rows accept text only; their attribution is declared by the producer.
        service.utter("room", Json.obj().put("speaker_id", "ava").put("start_ms", 3000).put("end_ms", 4000).put("text", "Bne, go ahead").put("source", "agent"));
        assertEquals(400, assertThrows(ApiException.class, () -> service.reviewUtterance("room", 2, review("Ben, go ahead", "a"))).status);
        var agent = service.reviewUtterance("room", 2, review("Ben, go ahead", null));
        assertEquals("agent", agent.path("label").asText()); assertEquals("ava", agent.path("speaker_id").asText()); assertEquals("Bne, go ahead", agent.path("original_text").asText());
        assertTrue(service.utterances("room", 0, 100, "high").path("text").asText().contains("Ava [agent]: Ben, go ahead"));
    }

    @Test void review_speakerChangeCorrectsExactlyTheSegmentsInsideTheSpan() throws Exception {
        engine.vector = new double[]{.8, .6};
        ingest("room", 12);   // sequences 0-4 buffer without an embedding; 5-11 are attributed to Alice with eligible embeddings
        double[] bobBefore = store.profiles("room").get(1).vector().clone();
        service.utter("room", human("a", 1500, 2500, "hello there", .3, .06));   // low label spanning sequences 6-9
        service.utter("room", human("a", 0, 1250, "buffering span", .3, .06));   // sequences 0-4, no embeddings
        assertEquals("low", service.utterances("room", 0, 100, null).path("utterances").get(0).path("label").asText());
        assertTrue(service.utterances("room", 0, 100, "high").path("utterances").isEmpty());
        clock.now += 5;
        var reviewed = service.reviewUtterance("room", 1, review(null, "b"));
        assertEquals("b", reviewed.path("speaker_id").asText()); assertEquals("Bob", reviewed.path("speaker_name").asText());
        assertEquals("reviewed", reviewed.path("label").asText()); assertEquals("a", reviewed.path("original_speaker_id").asText());
        assertEquals("hello there", reviewed.path("text").asText()); assertTrue(reviewed.path("original_text").isNull());
        assertEquals(4, reviewed.path("segments_corrected").asInt()); assertTrue(reviewed.path("profile_updated").asBoolean());
        assertEquals(4, count("corrections")); assertEquals(4, count("correction_examples"));
        try (var p = store.prepare("SELECT sequence,speaker_id,body FROM segments WHERE session_id='room' ORDER BY sequence"); var r = p.executeQuery()) {
            while (r.next()) {
                int sequence = r.getInt(1); boolean inside = sequence >= 6 && sequence <= 9; var body = Json.parse(r.getString(3));
                if (sequence < 5) { assertNull(r.getString(2)); continue; }
                assertEquals(inside ? "b" : "a", r.getString(2), "sequence " + sequence);
                assertEquals(inside ? "human_correction" : "model", body.path("source").asText(), "sequence " + sequence);
            }
        }
        assertFalse(Arrays.equals(bobBefore, store.profiles("room").get(1).vector()), "eligible corrections rebuilt Bob's profile");
        for (var correction : service.corrections("room", 0, 100).path("corrections")) { assertEquals("a", correction.path("previous_speaker").asText()); assertEquals("b", correction.path("actual_speaker").asText()); assertTrue(correction.path("profile_updated").asBoolean()); }
        // A human label survives every filter and reads as [reviewed]; the model's numbers stay on the row.
        var high = service.utterances("room", 0, 100, "high");
        assertEquals(1, high.path("utterances").size()); assertEquals("#1 0:01.5-0:02.5 Bob [reviewed]: hello there\n", high.path("text").asText());
        assertEquals(.3, high.path("utterances").get(0).path("similarity").asDouble()); assertEquals("similarity_based_uncalibrated", high.path("label_kind").asText());
        assertEquals("utterance_reviewed", lastEvent("room").path("type").asText()); assertEquals("reviewed", lastEvent("room").path("data").path("label").asText());
        assertEquals(high.path("utterances").get(0).toString(), lastEvent("room").path("data").toString());
        // Reviewing the same speaker again logs nothing new; a later text-only review keeps the reviewed speaker.
        assertEquals(0, service.reviewUtterance("room", 1, review(null, "b")).path("segments_corrected").asInt());
        assertEquals(4, count("corrections"));
        var again = service.reviewUtterance("room", 1, review("hello, there", null));
        assertEquals("b", again.path("speaker_id").asText()); assertEquals("reviewed", again.path("label").asText()); assertEquals("hello there", again.path("original_text").asText());
        // Buffering segments carry no embedding: corrections are logged but no profile changes.
        var buffered = service.reviewUtterance("room", 2, review(null, "b"));
        assertEquals(5, buffered.path("segments_corrected").asInt()); assertFalse(buffered.path("profile_updated").asBoolean());
        assertEquals(9, count("corrections")); assertEquals(4, count("correction_examples"));
    }

    @Test void review_restRouteNeedsAdmissionAndAPositiveUtteranceId() throws Exception {
        service.utter("room", human("a", 0, 1000, "typo hear", .7, .4));
        try (var api = new RestServer(service, 0, TOKEN)) {
            api.start(); String base = "http://127.0.0.1:" + api.port();
            var ok = request(base, "POST", "/speaker/session/room/utterances/1/review", review("typo here", "b"), Map.of());
            assertEquals(200, ok.statusCode());
            var body = Json.parse(ok.body());
            assertEquals("typo here", body.path("text").asText()); assertEquals("b", body.path("speaker_id").asText()); assertEquals("reviewed", body.path("label").asText());
            assertEquals(0, body.path("segments_corrected").asInt()); assertFalse(body.path("profile_updated").asBoolean());
            assertEquals(400, request(base, "POST", "/speaker/session/room/utterances/abc/review", review("x", null), Map.of()).statusCode());
            assertEquals(400, request(base, "POST", "/speaker/session/room/utterances/0/review", review("x", null), Map.of()).statusCode());
            assertEquals(404, request(base, "POST", "/speaker/session/room/utterances/999/review", review("x", null), Map.of()).statusCode());
            assertEquals(400, request(base, "POST", "/speaker/session/room/utterances/1/review", Json.obj(), Map.of()).statusCode());
            assertEquals(404, request(base, "GET", "/speaker/session/room/utterances/1/review", null, Map.of()).statusCode());
            assertEquals(401, request(base, "POST", "/speaker/session/room/utterances/1/review", review("x", null), Map.of("Authorization", "Bearer wrong")).statusCode());
            service.end("room");
            assertEquals(403, request(base, "POST", "/speaker/session/room/utterances/1/review", review("x", null), Map.of()).statusCode());
        }
    }

    @Test void retention_laterRoomsStartFromTheRetainedVoiceprintAndRefineIt() throws Exception {
        clock.now = ZonedDateTime.of(2028, 2, 29, 12, 0, 0, 0, ZoneOffset.UTC).toInstant().toEpochMilli();
        var first = PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("first"), PrivacyTestSupport.RETAIN_SCOPES);
        assertEquals(2, first.path("participants").size());
        for (var p : first.path("participants")) assertFalse(p.path("profile_seeded").asBoolean(), "nothing retained yet");
        var status = service.consentStatus("first");
        for (var p : status.path("participants")) assertTrue(p.path("retain_profile").asBoolean());
        assertFalse(status.path("scopes").has(PrivacyGate.RETENTION), "retention is per person, not a room-wide scope");
        assertTrue(status.path("scopes").path("negotiation_text").asBoolean());
        // A correction moves Bob's in-session vector away from his enrollment, so the retained voiceprint is a refinement.
        engine.vector = new double[]{.8, .6}; ingest("first", 6);
        service.correct("first", Json.obj().put("segment_id", service.current("first").path("segment_id").asText()).put("actual_speaker", "b"));
        double[] finalA = store.profiles("first").get(0).vector().clone(), finalB = store.profiles("first").get(1).vector().clone();
        assertFalse(Arrays.equals(finalB, new double[]{0, 1}));
        assertEquals(0, count("retained_profiles"));
        service.end("first");
        assertEquals(2, count("retained_profiles"));
        var bob = store.retainedProfile(KEY_B);
        assertEquals("Bob", bob.name()); assertEquals("test-model", bob.model()); assertEquals(1, bob.sessions());
        assertArrayEquals(finalB, bob.vector(), 1e-12); assertEquals(clock.now, bob.created()); assertEquals(clock.now, bob.lastInteraction());
        assertEquals(ZonedDateTime.of(2028, 2, 29, 12, 0, 1, 500_000_000, ZoneOffset.UTC).toInstant().toEpochMilli(), clock.now, "six ingested chunks moved the clock 1.5 s");
        assertEquals(ZonedDateTime.of(2031, 2, 28, 12, 0, 1, 500_000_000, ZoneOffset.UTC).toInstant().toEpochMilli(), bob.deadline(), "three calendar years, February 29 falling back to February 28");
        assertEquals(bob.deadline(), PrivacyGate.threeYears(clock.now));
        assertArrayEquals(finalA, store.retainedProfile(KEY_A).vector(), 1e-12);
        // Destruction of the room's graph leaves the separate retention class alone.
        service.sweepPrivacy();
        assertTrue(service.destructionStatus("first").path("verified").asBoolean());
        assertEquals(0, count("profiles")); assertEquals(2, count("retained_profiles"));
        // Same names and contacts, same model: enrollment starts from the blend, anchor and vector alike.
        clock.now += 60_000;
        var second = PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("second"), PrivacyTestSupport.RETAIN_SCOPES);
        for (var p : second.path("participants")) assertTrue(p.path("profile_seeded").asBoolean(), p.toString());
        var profiles = store.profiles("second");
        double[] expectedB = blend(new double[]{0, 1}, finalB);
        assertArrayEquals(expectedB, profiles.get(1).anchor(), 1e-12); assertArrayEquals(expectedB, profiles.get(1).vector(), 1e-12);
        assertArrayEquals(blend(new double[]{1, 0}, finalA), profiles.get(0).anchor(), 1e-12);
        assertFalse(Arrays.equals(new double[]{0, 1}, profiles.get(1).anchor()));
        assertEquals(1, store.retainedProfile(KEY_B).sessions(), "seeding reads, never writes");
        // Ending the second room blends again and counts the session.
        double[] finalB2 = store.profiles("second").get(1).vector().clone();
        service.end("second");
        bob = store.retainedProfile(KEY_B);
        assertEquals(2, bob.sessions()); assertArrayEquals(blend(finalB, finalB2), bob.vector(), 1e-12);
        assertEquals(clock.now, bob.lastInteraction()); assertEquals(PrivacyGate.threeYears(clock.now), bob.deadline());
        assertEquals(clock.now - 60_000, bob.created());
        service.end("second"); assertEquals(2, store.retainedProfile(KEY_B).sessions(), "a repeated end does not count twice");
        assertEquals(2, count("retained_profiles"));
        // The subject key is the operator-entered identity, folded and trimmed.
        assertEquals(KEY_A, PrivacyGate.subjectKey(" ALICE ", "A@Example.Invalid "));
        assertEquals(PrivacyPolicy.sha256("alice\na@example.invalid".getBytes(StandardCharsets.UTF_8)), KEY_A);
        assertNotEquals(KEY_A, PrivacyGate.subjectKey("Alice", "alice@example.invalid"));
    }

    @Test void retention_modelMismatchIsIgnoredAndAnUnscopedPersonIsNeverTouched() throws Exception {
        var roster = SpeakerServiceTest.initRequest("mixed").path("participants");
        var room = Json.obj().put("session_id", "mixed").put("purpose_id", PrivacyPolicy.PURPOSE);
        for (var p : roster) room.withArray("participants").add(Json.obj().put("id", p.path("id").asText()).put("name", p.path("name").asText()).put("contact", p.path("id").asText() + "@example.invalid"));
        service.createPrivacyRoom(room);
        PrivacyTestSupport.sign(service, "mixed", "a", "Alice", PrivacyTestSupport.RETAIN_SCOPES);
        PrivacyTestSupport.sign(service, "mixed", "b", "Bob", PrivacyTestSupport.ALL_SCOPES);
        var status = service.consentStatus("mixed");
        assertTrue(status.path("allowed").asBoolean());
        assertTrue(status.path("participants").get(0).path("retain_profile").asBoolean()); assertFalse(status.path("participants").get(1).path("retain_profile").asBoolean());
        assertTrue(status.path("scopes").path("hosted_mcp").asBoolean(), "one person's retention choice does not affect the room's disclosures");
        service.init(SpeakerServiceTest.initRequest("mixed"));
        service.end("mixed");
        assertEquals(1, count("retained_profiles")); assertNull(store.retainedProfile(KEY_B)); assertNotNull(store.retainedProfile(KEY_A));
        // A retained row from another model neither seeds nor is deleted.
        store.execute("UPDATE retained_profiles SET model='other-model'");
        var second = PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("second"), PrivacyTestSupport.RETAIN_SCOPES);
        for (var p : second.path("participants")) assertFalse(p.path("profile_seeded").asBoolean());
        assertArrayEquals(new double[]{1, 0}, store.profiles("second").get(0).anchor());
        var alice = store.retainedProfile(KEY_A);
        assertEquals("other-model", alice.model()); assertEquals(1, alice.sessions()); assertArrayEquals(new double[]{1, 0}, alice.vector());
        // Purpose completion replaces a vector from a different model instead of blending across models.
        service.end("second");
        alice = store.retainedProfile(KEY_A);
        assertEquals("test-model", alice.model()); assertEquals(2, alice.sessions()); assertEquals(1, store.retainedProfile(KEY_B).sessions());
        // Withdrawal, deletion requests and the sweeper never write a voiceprint.
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("third"), PrivacyTestSupport.RETAIN_SCOPES);
        service.delete("third"); service.sweepPrivacy();
        assertEquals(2, store.retainedProfile(KEY_A).sessions()); assertEquals(1, store.retainedProfile(KEY_B).sessions());
        assertEquals(2, count("retained_profiles"));
    }

    @Test void retention_withdrawalDeletesOnlyTheWithdrawingPersonAndExpiryIsSwept() throws Exception {
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("first"), PrivacyTestSupport.RETAIN_SCOPES);
        service.end("first");
        assertEquals(2, count("retained_profiles"));
        // Alice withdraws from a later room, even one where she did not choose retention: her voice is not kept because Bob still consents.
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("second"), PrivacyTestSupport.ALL_SCOPES);
        var revoked = service.consentRevoke("second", "a");
        assertEquals("destroying", revoked.path("state").asText());
        assertNull(store.retainedProfile(KEY_A)); assertNotNull(store.retainedProfile(KEY_B));
        assertEquals(1, store.retainedProfile(KEY_B).sessions(), "withdrawal writes nothing for anyone");
        try (var p = store.prepare("SELECT count(*) FROM consent_audit WHERE action='voiceprint_deleted' AND participant_id='a'"); var r = p.executeQuery()) { assertEquals(1, r.getLong(1)); }
        service.sweepPrivacy();
        assertEquals(1, count("retained_profiles"));
        // A room whose 30-minute deadline has passed is expiry, not purpose completion: ending it writes nothing.
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("stale"), PrivacyTestSupport.RETAIN_SCOPES);
        clock.now += PrivacyPolicy.INACTIVITY_MS;
        service.end("stale");
        assertEquals(1, count("retained_profiles")); assertNull(store.retainedProfile(KEY_A));
        // Deadline expiry: the sweeper removes the row at its deadline, not before.
        long deadline = store.retainedProfile(KEY_B).deadline();
        clock.now = deadline - 1; service.sweepPrivacy(); assertEquals(1, count("retained_profiles"));
        clock.now = deadline; service.sweepPrivacy(); assertEquals(0, count("retained_profiles"));
    }

    @Test void retention_profilesAreListedWithoutVectorsAndDeletedOnlyFromTheLocalOrigin() throws Exception {
        PrivacyTestSupport.init(service, SpeakerServiceTest.initRequest("first"), PrivacyTestSupport.RETAIN_SCOPES);
        service.end("first");
        try (var api = new RestServer(service, 0, TOKEN)) {
            api.start(); String base = "http://127.0.0.1:" + api.port();
            var origin = Map.of("Origin", base);
            var listed = request(base, "GET", "/privacy/profiles", null, Map.of());
            assertEquals(200, listed.statusCode());
            var profiles = Json.parse(listed.body()).path("profiles");
            assertEquals(2, profiles.size());
            assertFalse(listed.body().contains("vector")); assertFalse(listed.body().contains("1.0,0.0"));
            for (var row : profiles) {
                var fields = new ArrayList<String>(); row.fieldNames().forEachRemaining(fields::add);
                assertEquals(List.of("subject_key", "subject_name", "model", "sessions", "created_ms", "last_interaction_ms", "retention_deadline_ms"), fields);
                assertEquals(1, row.path("sessions").asInt()); assertEquals("test-model", row.path("model").asText());
                assertEquals(PrivacyGate.threeYears(clock.now), row.path("retention_deadline_ms").asLong());
            }
            assertEquals(401, request(base, "GET", "/privacy/profiles", null, Map.of("Authorization", "Bearer wrong")).statusCode());
            assertEquals(403, request(base, "DELETE", "/privacy/profiles/" + KEY_A, null, Map.of()).statusCode(), "the person clicks the delete: local browser origin required");
            assertEquals(2, count("retained_profiles"));
            assertEquals(400, request(base, "DELETE", "/privacy/profiles/not-a-key", null, origin).statusCode());
            var deleted = request(base, "DELETE", "/privacy/profiles/" + KEY_A, null, origin);
            assertEquals(200, deleted.statusCode()); assertTrue(Json.parse(deleted.body()).path("deleted").asBoolean());
            assertEquals(404, request(base, "DELETE", "/privacy/profiles/" + KEY_A, null, origin).statusCode());
            assertEquals(1, Json.parse(request(base, "GET", "/privacy/profiles", null, Map.of()).body()).path("profiles").size());
            assertEquals("Bob", Json.parse(request(base, "GET", "/privacy/profiles", null, Map.of()).body()).path("profiles").get(0).path("subject_name").asText());
        }
    }

    @Test void notice_namesTheConfiguredProvidersAndOtherPossibleRecipients() {
        var notice = service.privacyNotice();
        String text = notice.path("notice_text").asText();
        assertTrue(text.contains("currently OpenAI through Cloudflare's tunnel for hosted MCP"));
        assertTrue(text.contains("xAI")); assertTrue(text.contains("Google Gemini"));
        assertTrue(text.contains("openai_ prefix in scope identifiers is historical"));
        assertTrue(text.contains(PrivacyPolicy.RETENTION_SENTENCE.strip()));
        assertTrue(text.contains("negotiation_text")); assertFalse(text.contains("to OpenAI text models")); assertFalse(text.contains("to OpenAI Realtime"));
        assertEquals(1, notice.path("providers").size()); assertEquals("OpenAI", notice.path("providers").get(0).asText());
        assertTrue(notice.path("retention_text").asText().contains("voice_profile_retention"));
        assertTrue(notice.path("retention_text").asText().contains("February 28"));
        assertEquals(PrivacyPolicy.sha256(text.getBytes(StandardCharsets.UTF_8)), notice.path("notice_sha256").asText());
        // The configured list is rendered into the text, so the hash tracks it and earlier releases no longer match.
        var two = new PrivacyPolicy("Synthetic Test Operator", "1 Test Street", "operator@example.invalid", TOKEN, true, true, PrivacyPolicy.providers(" OpenAI , Google Gemini,, "));
        assertEquals(List.of("OpenAI", "Google Gemini"), two.providers());
        assertTrue(two.noticeText().contains("currently OpenAI, Google Gemini through Cloudflare"));
        assertNotEquals(notice.path("notice_sha256").asText(), two.noticeHash());
        assertEquals(2, two.notice().path("providers").size());
        assertEquals(List.of("OpenAI"), PrivacyPolicy.providers(null)); assertEquals(List.of("OpenAI"), PrivacyPolicy.providers(" , "));
        var reconfigured = new SpeakerService(store, engine, clock, two);
        assertFalse(reconfigured.consentStatus("room").path("allowed").asBoolean(), "a release signed under the old provider list is stale");
        // The retention scope is accepted only as a release scope; an unknown one is still refused.
        assertEquals(403, assertThrows(ApiException.class, () -> {
            PrivacyTestSupport.authorize(service, "bad", SpeakerServiceTest.initRequest("bad").path("participants"), List.of("voice_profile_retention", "everything"));
        }).status);
    }
}
