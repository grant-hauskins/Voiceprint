package dev.voiceprint;

import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import java.nio.file.Path;
import java.time.*;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class SpeakerServiceTest {
    @TempDir Path temp;
    Store store;
    FakeEngine engine;
    SpeakerService service;
    MutableClock clock;
    static final String AUDIO = Base64.getEncoder().encodeToString(new byte[8000]);
    static final String OPENING = Base64.getEncoder().encodeToString(new byte[160000]);
    @BeforeEach void setup() throws Exception {
        store = new Store(temp.resolve("test.sqlite")); engine = new FakeEngine(); clock = new MutableClock();
        service = new SpeakerService(store, engine, clock);
    }
    @AfterEach void close() throws Exception { store.close(); }
    static ObjectNode initRequest(String id) {
        var n = Json.obj().put("session_id", id).put("sample_rate", 16000).put("audio_format", "pcm_s16le");
        var participants = n.putArray("participants");
        participants.add(Json.obj().put("id", "a").put("name", "Alice").put("opening_statement_audio", OPENING));
        participants.add(Json.obj().put("id", "b").put("name", "Bob").put("opening_statement_audio", OPENING)); return n;
    }
    ObjectNode frame(int sequence) { return Json.obj().put("sequence", sequence).put("audio_base64", AUDIO); }
    ObjectNode warmup() { ObjectNode n = null; for (int i = 0; i < 6; i++) { n = service.ingest("test", frame(i)); clock.now += 250; } return n; }
    @Test void enrollmentIsAtomicAndCreatesDistinctProfiles() {
        assertEquals(2, service.init(initRequest("test")).get("profiles_created").asInt());
        assertEquals(2, service.profiles("test").get("participants").size());
        assertEquals(409, assertThrows(ApiException.class, () -> service.init(initRequest("test"))).status);
    }
    @Test void failedEnrollmentDoesNotLeavePartialSession() {
        engine.failEnrollmentAt = 2;
        assertThrows(ApiException.class, () -> service.init(initRequest("test")));
        assertEquals(404, assertThrows(ApiException.class, () -> service.profiles("test")).status);
    }
    @Test void duplicateParticipantsRejectedBeforeInference() {
        var n = initRequest("test"); ((ObjectNode) n.get("participants").get(1)).put("id", "a");
        assertEquals(400, assertThrows(ApiException.class, () -> service.init(n)).status);
        assertEquals(0, engine.enrollments);
    }
    @Test void buffersThenAttributesWithoutInventingProbability() {
        service.init(initRequest("test"));
        var result = warmup();
        assertEquals("a", result.get("speaker_id").asText());
        assertTrue(result.get("confidence").isNull()); assertTrue(result.get("uncertain").asBoolean());
        assertFalse(result.get("trusted").asBoolean()); assertEquals(1, engine.calls);
        assertEquals(1250, result.get("start_ms").asInt()); assertEquals(0, result.get("context_start_ms").asInt());
    }
    @Test void sequenceRetriesAreIdempotentAndGapsRejected() {
        service.init(initRequest("test")); var original = warmup();
        assertEquals(original.toString(), service.ingest("test", frame(5)).toString()); assertEquals(1, engine.calls);
        assertEquals(409, assertThrows(ApiException.class, () -> service.ingest("test", frame(8))).status);
        assertEquals(409, assertThrows(ApiException.class, () -> service.ingest("test", frame(5).put("text", "changed"))).status);
    }
    @Test void failureIsNotLowConfidenceAndCanRetryWithoutLosingAudio() {
        service.init(initRequest("test")); warmup(); engine.fail = true;
        assertEquals(503, assertThrows(ApiException.class, () -> service.ingest("test", frame(6))).status);
        assertEquals("inference_error", service.current("test").get("status").asText());
        assertTrue(service.current("test").get("speaker_id").isNull());
        engine.fail = false; assertEquals(6, service.ingest("test", frame(6)).get("sequence").asInt());
    }
    @Test void staleAndEndedSessionsNeverReportCurrentSpeaker() {
        service.init(initRequest("test")); warmup(); clock.now += 2000;
        assertEquals("stale", service.current("test").get("status").asText());
        assertEquals("buffering", service.ingest("test", frame(6)).get("status").asText());
        service.end("test"); assertEquals("ended", service.current("test").get("status").asText());
        assertThrows(ApiException.class, () -> service.ingest("test", frame(7)));
    }
    @Test void correctionChangesPersistedProfileAndFutureMatchWithoutDoubleCounting() {
        service.init(initRequest("test")); engine.vector = new double[]{.8, .6}; var original = warmup();
        double[] before = store.profiles("test").get(1).vector().clone();
        var correction = Json.obj().put("segment_id", original.get("segment_id").asText()).put("actual_speaker", "b");
        assertTrue(service.correct("test", correction).get("profile_updated").asBoolean());
        double[] after = store.profiles("test").get(1).vector(); assertFalse(Arrays.equals(before, after));
        service.correct("test", correction); assertArrayEquals(after, store.profiles("test").get(1).vector());
        assertEquals(1, service.corrections("test", 0, 100).get("corrections").size());
        engine.vector = new double[]{.71, .70};
        assertEquals("b", service.ingest("test", frame(6)).get("speaker_id").asText());
        service.correct("test", correction.put("actual_speaker", "a"));
        assertArrayEquals(before, store.profiles("test").get(1).vector(), 1e-10);
    }
    @Test void overlapAbstainsAndCannotContaminateProfile() {
        service.init(initRequest("test")); engine.overlap = "detected"; var result = warmup();
        assertEquals("overlap", result.get("status").asText()); assertTrue(result.get("speaker_id").isNull());
        assertEquals(2, result.get("candidates").size());
        double[] before = store.profiles("test").get(1).vector().clone();
        var response = service.correct("test", Json.obj().put("segment_id", result.get("segment_id").asText()).put("actual_speaker", "b"));
        assertTrue(response.get("correction_logged").asBoolean()); assertFalse(response.get("profile_updated").asBoolean());
        assertArrayEquals(before, store.profiles("test").get(1).vector());
    }
    @Test void silenceAndSpeakerChangeDoNotGuessIdentity() {
        service.init(initRequest("test")); engine.speech = false;
        assertEquals("silence", warmup().get("status").asText());
        engine.speech = true; engine.change = true;
        assertTrue(service.ingest("test", frame(6)).get("speaker_id").isNull());
    }
    @Test void calibrationThresholdAndHumanCorrectionDoNotReuseOldProbability() {
        service.init(initRequest("test")); engine.probability = .59;
        assertTrue(warmup().get("uncertain").asBoolean()); engine.probability = .94;
        var result = service.ingest("test", frame(6)); assertTrue(result.get("trusted").asBoolean());
        var response = service.correct("test", Json.obj().put("segment_id", result.get("segment_id").asText()).put("actual_speaker", "b"));
        assertTrue(response.path("attribution").get("confidence").isNull());
        assertEquals(.94, response.path("attribution").get("original_confidence").asDouble());
    }
    @Test void persistenceAndScopedQueriesSurviveRestart() throws Exception {
        service.init(initRequest("test")); var result = warmup();
        service.correct("test", Json.obj().put("segment_id", result.get("segment_id").asText()).put("actual_speaker", "b"));
        store.close(); store = new Store(temp.resolve("test.sqlite")); service = new SpeakerService(store, engine, clock);
        assertEquals(1, service.transcript("test", "b", -1, 100).get("transcript").size());
        assertEquals(1, service.corrections("test", 0, 100).get("corrections").size());
        assertEquals("buffering", service.ingest("test", frame(6)).get("status").asText());
        assertThrows(ApiException.class, () -> service.transcript("test", null, -1, 201));
        service.delete("test"); assertThrows(ApiException.class, () -> service.current("test"));
    }
    @Test void utterancesStoreExternalTextOrderedByTimeWithCompactLines() {
        service.init(initRequest("test"));
        assertEquals(1, service.utter("test", Json.obj().put("speaker_id", "b").put("start_ms", 4000).put("end_ms", 6500).put("text", "second words")).get("utterance_id").asLong());
        service.utter("test", Json.obj().put("speaker_id", "a").put("start_ms", 1000).put("end_ms", 3500).put("text", "first words"));
        service.utter("test", Json.obj().putNull("speaker_id").put("start_ms", 7000).put("end_ms", 8000).put("text", "mystery"));
        assertEquals(404, assertThrows(ApiException.class, () -> service.utter("test", Json.obj().put("speaker_id", "zed").put("start_ms", 0).put("end_ms", 1).put("text", "x"))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.utter("test", Json.obj().put("speaker_id", "a").put("start_ms", 5).put("end_ms", 5).put("text", "x"))).status);
        var page = service.utterances("test", 0, 100, null);
        assertEquals("#2 0:01.0-0:03.5 Alice [unknown]: first words\n#1 0:04.0-0:06.5 Bob [unknown]: second words\n#3 0:07.0-0:08.0 unknown [unknown]: mystery\n", page.get("text").asText());
        assertEquals(3, page.get("next_after_id").asLong());
        assertEquals(0, service.utterances("test", 3, 100, null).get("utterances").size());
        var sessions = service.sessions(10).get("sessions");
        assertEquals("test", sessions.get(0).get("session_id").asText());
        assertEquals("a=Alice, b=Bob", sessions.get(0).get("participants").asText());
    }
    @Test void labelsAreSimilarityBasedAndOverlapRowsNameBothCandidates() {
        assertEquals("high", SpeakerService.label("a", .70, .40, 0.0, 0.0, 0));
        assertEquals("medium", SpeakerService.label("a", .54, .40, 0.0, 0.0, 0));   // similarity just under the high bar
        assertEquals("medium", SpeakerService.label("a", .70, .24, 0.0, 0.0, 0));   // margin just under the high bar
        assertEquals("medium", SpeakerService.label("a", .70, .40, .15, 0.0, 0));   // some overlap in the window
        assertEquals("low", SpeakerService.label("a", .70, .40, 0.0, .5, 0));       // half the chunks abstained
        assertEquals("low", SpeakerService.label("a", .30, .06, 0.0, 0.0, 0));
        assertEquals("overlap", SpeakerService.label("a", .70, .40, .3, 0.0, 0));
        assertEquals("overlap", SpeakerService.label(null, null, null, null, null, 2));
        assertEquals("unknown", SpeakerService.label(null, null, null, null, null, 0));
        assertEquals("unknown", SpeakerService.label("a", null, null, 0.0, 0.0, 0));
        service.init(initRequest("test"));
        service.utter("test", Json.obj().put("speaker_id", "a").put("start_ms", 0).put("end_ms", 2000).put("text", "clean").put("similarity", .7).put("margin", .4).put("overlap_ratio", 0).put("abstain_ratio", 0));
        var overlap = Json.obj().putNull("speaker_id").put("start_ms", 2000).put("end_ms", 3000).put("text", "both").put("overlap_ratio", 1.0);
        overlap.putArray("candidates").add("a").add("b");
        service.utter("test", overlap);
        service.utter("test", Json.obj().put("speaker_id", "b").put("start_ms", 3000).put("end_ms", 5000).put("text", "weak").put("similarity", .3).put("margin", .06).put("overlap_ratio", 0).put("abstain_ratio", .1));
        assertEquals(404, assertThrows(ApiException.class, () -> service.utter("test", Json.obj().putNull("speaker_id").put("start_ms", 0).put("end_ms", 1).put("text", "x").set("candidates", Json.arr().add("zed")))).status);
        assertEquals(400, assertThrows(ApiException.class, () -> service.utter("test", Json.obj().put("speaker_id", "a").put("start_ms", 0).put("end_ms", 1).put("text", "x").put("overlap_ratio", 2))).status);
        service.utter("test", Json.obj().put("speaker_id", "b").put("start_ms", 5000).put("end_ms", 6000).put("text", "partly").put("similarity", .6).put("margin", .3).put("overlap_ratio", .4).put("abstain_ratio", 0));
        var all = service.utterances("test", 0, 100, null);
        assertEquals("#1 0:00.0-0:02.0 Alice [high]: clean\n#2 0:02.0-0:03.0 OVERLAP Alice+Bob [overlap 100%]: both\n#3 0:03.0-0:05.0 Bob [low]: weak\n#4 0:05.0-0:06.0 Bob [overlap 40%]: partly\n", all.get("text").asText());
        assertEquals("similarity_based_uncalibrated", all.get("label_kind").asText());
        assertEquals(.7, all.get("utterances").get(0).get("similarity").asDouble());
        assertEquals(2, all.get("utterances").get(1).get("candidates").size());
        assertEquals("#1 0:00.0-0:02.0 Alice [high]: clean\n", service.utterances("test", 0, 100, "high").get("text").asText());
        assertEquals(2, service.utterances("test", 0, 100, "low").get("utterances").size());
        assertEquals(400, assertThrows(ApiException.class, () -> service.utterances("test", 0, 100, "certain")).status);
    }
    @Test void version2DatabaseGainsUncertaintyColumns() throws Exception {
        store.close();
        try (var db = java.sql.DriverManager.getConnection("jdbc:sqlite:" + temp.resolve("old.sqlite").toAbsolutePath()); var s = db.createStatement()) {
            s.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,status TEXT NOT NULL,created_ms INTEGER NOT NULL,next_sequence INTEGER NOT NULL DEFAULT 0,elapsed_ms INTEGER NOT NULL DEFAULT 0)");
            s.execute("CREATE TABLE profiles(session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,id TEXT NOT NULL,name TEXT NOT NULL,model TEXT NOT NULL,anchor TEXT NOT NULL,vector TEXT NOT NULL,PRIMARY KEY(session_id,id))");
            s.execute("CREATE TABLE utterances(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,speaker_id TEXT,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,text TEXT NOT NULL,source TEXT NOT NULL,created_ms INTEGER NOT NULL)");
            s.execute("INSERT INTO sessions(id,status,created_ms) VALUES('old','ended',1)");
            s.execute("INSERT INTO profiles VALUES('old','a','Alice','m','[1]','[1]')");
            s.execute("INSERT INTO utterances(session_id,speaker_id,start_ms,end_ms,text,source,created_ms) VALUES('old','a',0,1000,'legacy','x',1)");
            s.execute("PRAGMA user_version=2");
        }
        store = new Store(temp.resolve("old.sqlite")); service = new SpeakerService(store, engine, clock);
        assertEquals("#1 0:00.0-0:01.0 Alice [unknown]: legacy\n", service.utterances("old", 0, 100, null).get("text").asText());
        try (var s = store.db.createStatement(); var r = s.executeQuery("PRAGMA user_version")) { assertEquals(3, r.getInt(1)); }
    }
    @Test void malformedAudioAndNonintegralSequenceRejected() {
        service.init(initRequest("test"));
        assertThrows(ApiException.class, () -> service.ingest("test", frame(0).put("audio_base64", "bad")));
        assertThrows(ApiException.class, () -> service.ingest("test", frame(0).put("sequence", .1)));
    }
    static class MutableClock extends Clock {
        long now = 1_000_000;
        public ZoneId getZone() { return ZoneOffset.UTC; }
        public Clock withZone(ZoneId zone) { return this; }
        public Instant instant() { return Instant.ofEpochMilli(now); }
        public long millis() { return now; }
    }
    static class FakeEngine implements SpeechEngine {
        int enrollments, calls, failEnrollmentAt = -1;
        boolean fail, speech = true, change;
        String overlap = "clear";
        double[] vector = {1, 0}; Double probability;
        public Result analyze(byte[] pcm, boolean enrollment) {
            if (enrollment) {
                enrollments++;
                if (enrollments == failEnrollmentAt) throw new ApiException(503, "inference_unavailable", "Test failure");
                return new Result("test-model", enrollments % 2 == 1 ? new double[]{1, 0} : new double[]{0, 1}, true, "clear", false, true);
            }
            calls++; if (fail) throw new ApiException(503, "inference_unavailable", "Test failure");
            return new Result("test-model", Audio.normalize(vector), speech, overlap, change, true);
        }
        public Match match(Result result, List<Store.Profile> profiles) {
            var candidates = profiles.stream().map(p -> new Candidate(p.id(), Audio.cosine(result.embedding(), p.vector())))
                .sorted(Comparator.comparingDouble(Candidate::similarity).reversed()).toList();
            return new Match(candidates, probability, probability == null ? null : "test-calibration");
        }
    }
}
