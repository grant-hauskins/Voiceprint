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
