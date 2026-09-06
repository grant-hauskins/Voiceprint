package dev.voiceprint;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.*;
import java.security.*;
import java.time.Clock;
import java.util.*;

/** Serializes mutation and inference for the single-worker MVP; clients must apply backpressure. */
final class SpeakerService {
    private final Store store;
    private final SpeechEngine engine;
    private final Clock clock;
    private final Map<String, byte[]> buffers = new HashMap<>();
    private final Map<String, String> failures = new HashMap<>();
    SpeakerService(Store store, SpeechEngine engine, Clock clock) { this.store = store; this.engine = engine; this.clock = clock; }

    synchronized ObjectNode init(JsonNode request) {
        String session = Json.id(request, "session_id");
        if (Json.integer(request, "sample_rate", 16000, 16000) != 16000 || !"pcm_s16le".equals(Json.text(request, "audio_format", 20)))
            throw new ApiException(400, "invalid_audio_format", "Expected mono 16 kHz pcm_s16le.");
        JsonNode participants = request.path("participants");
        if (!participants.isArray() || participants.size() < 2 || participants.size() > 4)
            throw new ApiException(400, "invalid_participants", "Enroll 2–4 participants.");
        try { store.session(session); throw new ApiException(409, "session_exists", "Session already exists."); }
        catch (ApiException e) { if (e.status != 404) throw e; }
        try (var p = store.prepare("SELECT count(*) FROM sessions WHERE status='active'"); var r = p.executeQuery()) {
            if (r.getInt(1) >= 32) throw new ApiException(429, "session_limit", "End an existing session before creating another.");
        } catch (java.sql.SQLException e) { throw new IllegalStateException(e); }
        var ids = new HashSet<String>(); var audio = new ArrayList<byte[]>();
        for (JsonNode participant : participants) {
            if (!ids.add(Json.id(participant, "id"))) throw new ApiException(400, "duplicate_participant", "Participant IDs must be distinct.");
            Json.text(participant, "name", 120);
            audio.add(Audio.decode(Json.text(participant, "opening_statement_audio", 640000), true));
        }
        var results = new ArrayList<SpeechEngine.Result>();
        for (byte[] pcm : audio) {
            var result = engine.analyze(pcm, true);
            if (!result.speech() || result.embedding() == null || result.overlap().equals("detected") || result.speakerChange())
                throw new ApiException(422, "invalid_enrollment", "Each opening statement must contain one participant speaking clearly.");
            if (!results.isEmpty() && (!results.getFirst().modelId().equals(result.modelId()) || results.getFirst().embedding().length != result.embedding().length))
                throw new ApiException(503, "model_mismatch", "The speech model changed during enrollment.");
            results.add(result);
        }
        return store.transaction(() -> {
            store.execute("INSERT INTO sessions(id,status,created_ms) VALUES(?,'active',?)", session, clock.millis());
            for (int i = 0; i < participants.size(); i++) {
                var p = participants.get(i); var result = results.get(i); String vector = Store.vectorJson(Audio.normalize(result.embedding()));
                store.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?)", session, p.get("id").asText(), p.get("name").asText(), result.modelId(), vector, vector);
            }
            return Json.obj().put("session_id", session).put("profiles_created", participants.size()).put("status", "ready")
                .put("confidence_kind", "uncalibrated").put("chunk_ms", 250).put("context_ms", 1500);
        });
    }

    synchronized ObjectNode ingest(String session, JsonNode request) {
        var s = store.session(session);
        long sequence = Json.integer(request, "sequence", 0, Integer.MAX_VALUE);
        String encoded = Json.text(request, "audio_base64", 12000);
        byte[] pcm = Audio.decode(encoded, false);
        String text = null;
        if (request.has("text") && !request.get("text").isNull()) text = Json.text(request, "text", 4000);
        String hash = hash(pcm, text);
        if (sequence < s.nextSequence()) {
            try (var q = store.prepare("SELECT hash,body FROM segments WHERE session_id=? AND sequence=?", session, sequence); var r = q.executeQuery()) {
                if (r.next() && hash.equals(r.getString(1))) return (ObjectNode) Json.parse(r.getString(2));
                throw new ApiException(409, "sequence_conflict", "A different payload already occupies this sequence.");
            } catch (java.sql.SQLException e) { throw new IllegalStateException(e); }
        }
        if (!s.status().equals("active")) throw new ApiException(409, "session_ended", "This session has ended.");
        if (sequence != s.nextSequence()) throw new ApiException(409, "sequence_gap", "Expected sequence " + s.nextSequence() + ".");
        byte[] prior = buffers.getOrDefault(session, new byte[0]);
        ObjectNode last = s.nextSequence() == 0 ? null : store.segment(session, s.nextSequence() - 1);
        if (last != null && clock.millis() - last.path("timestamp_ms").asLong() > 1500) prior = new byte[0];
        byte[] context = Audio.append(prior, pcm);
        long started = System.nanoTime();
        SpeechEngine.Result result = null;
        if (context.length >= Audio.CONTEXT_BYTES) {
            try { result = engine.analyze(context, false); }
            catch (ApiException e) { failures.put(session, e.code); throw e; }
        }
        long inferenceMs = (System.nanoTime() - started) / 1_000_000;
        ObjectNode attribution;
        try { attribution = attribute(session, sequence, s.elapsedMs(), context.length / 32, result, inferenceMs); }
        catch (ApiException e) { failures.put(session, e.code); throw e; }
        attribution.put("text", text).put("text_source", text == null ? null : "client_supplied");
        SpeechEngine.Result completed = result;
        store.transaction(() -> {
            boolean eligible = completed != null && completed.embedding() != null && completed.speech() && completed.profileEligible()
                && completed.overlap().equals("clear") && !completed.speakerChange();
            store.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,?)", attribution.get("segment_id").asText(), session, sequence, hash,
                attribution.path("speaker_id").isNull() ? null : attribution.path("speaker_id").asText(), attribution.toString(),
                completed == null || completed.embedding() == null ? null : Store.vectorJson(completed.embedding()), eligible);
            store.execute("UPDATE sessions SET next_sequence=next_sequence+1,elapsed_ms=elapsed_ms+250 WHERE id=?", session);
            return null;
        });
        buffers.put(session, context); failures.remove(session);
        return attribution;
    }

    private ObjectNode attribute(String session, long sequence, long start, long contextMs, SpeechEngine.Result result, long inferenceMs) {
        var n = Json.obj().put("segment_id", UUID.randomUUID().toString()).put("session_id", session).put("sequence", sequence)
            .put("start_ms", start).put("end_ms", start + 250).put("context_start_ms", start + 250 - contextMs)
            .put("timestamp_ms", clock.millis()).putNull("speaker_id").putNull("original_speaker_id").putNull("confidence")
            .put("confidence_kind", "uncalibrated").put("uncertain", true).put("trusted", false).put("source", "model")
            .put("inference_ms", inferenceMs).put("context_ms", contextMs);
        var reasons = n.putArray("uncertainty_reasons"); var candidates = n.putArray("candidates");
        if (result == null) { n.put("status", "buffering").put("overlap", "unavailable"); reasons.add("insufficient_context"); return n; }
        n.put("model_id", result.modelId()).put("overlap", result.overlap());
        if (!result.speech()) { n.put("status", "silence"); reasons.add("no_speech"); return n; }
        if (!result.overlap().equals("clear")) reasons.add(result.overlap().equals("detected") ? "overlap" : "overlap_detection_unavailable");
        if (result.speakerChange()) reasons.add("speaker_change_in_context");
        var profiles = store.profiles(session);
        for (Store.Profile p : profiles) {
            if (!p.model().equals(result.modelId())) throw new ApiException(503, "model_mismatch", "Re-enroll participants after changing the model.");
            if (p.vector().length != result.embedding().length) throw new ApiException(503, "model_mismatch", "The speech model embedding dimensions changed.");
        }
        long matchingStarted = System.nanoTime();
        var match = engine.match(result, profiles);
        n.put("inference_ms", inferenceMs + (System.nanoTime() - matchingStarted) / 1_000_000);
        var ranked = match.candidates();
        for (var c : ranked) candidates.add(Json.obj().put("speaker_id", c.id()).put("similarity", c.similarity()).putNull("confidence").put("uncertain", true));
        double best = ranked.getFirst().similarity();
        double margin = best - ranked.get(1).similarity();
        n.put("similarity", best).put("margin", margin);
        if (best < .25 || margin < .05) reasons.add("weak_or_ambiguous_match");
        boolean canAttribute = best >= .25 && margin >= .05 && !result.overlap().equals("detected") && !result.speakerChange();
        if (canAttribute) {
            String speaker = ranked.getFirst().id();
            n.put("speaker_id", speaker).put("original_speaker_id", speaker).put("status", "tentative");
        } else n.put("status", result.overlap().equals("detected") ? "overlap" : "unknown");
        if (canAttribute && result.overlap().equals("clear") && match.confidence() != null) {
            double confidence = match.confidence();
            n.put("confidence", confidence).put("confidence_kind", "calibrated").put("calibration_id", match.calibrationId());
            if (confidence < .60) reasons.add("low_confidence");
            else n.put("uncertain", false).put("trusted", true).put("status", "attributed");
        } else reasons.add("uncalibrated_confidence");
        return n;
    }

    synchronized ObjectNode current(String session) {
        var s = store.session(session);
        ObjectNode last = s.nextSequence() == 0 ? null : store.segment(session, s.nextSequence() - 1);
        if (!s.status().equals("active") || failures.containsKey(session) || last == null || clock.millis() - last.path("timestamp_ms").asLong() > 1500) {
            String status = !s.status().equals("active") ? "ended" : failures.containsKey(session) ? "inference_error" : last == null ? "waiting" : "stale";
            var n = Json.obj().put("session_id", session).put("status", status).putNull("speaker_id").putNull("confidence")
                .put("confidence_kind", "unavailable").put("uncertain", true).put("trusted", false).put("timestamp_ms", clock.millis());
            n.putArray("uncertainty_reasons").add(status);
            if (last != null) n.put("last_segment_id", last.path("segment_id").asText());
            return n;
        }
        return last;
    }

    synchronized ObjectNode transcript(String session, String speaker, long after, int limit) {
        store.session(session); validatePage(after, limit);
        if (speaker != null && store.profiles(session).stream().noneMatch(p -> p.id().equals(speaker))) throw new ApiException(404, "speaker_not_found", "Speaker is not enrolled in this session.");
        ArrayNode rows = store.transcript(session, speaker, after, limit);
        return Json.obj().put("session_id", session).put("next_after_sequence", rows.isEmpty() ? after : rows.get(rows.size() - 1).path("sequence").asLong()).set("transcript", rows);
    }
    synchronized ObjectNode profiles(String session) {
        store.session(session); var profiles = Json.arr();
        for (var p : store.profiles(session)) profiles.add(Json.obj().put("id", p.id()).put("name", p.name()).put("model_id", p.model()));
        return Json.obj().put("session_id", session).set("participants", profiles);
    }
    synchronized ObjectNode corrections(String session, long after, int limit) {
        store.session(session); validatePage(after, limit);
        ArrayNode rows = store.corrections(session, after, limit);
        return Json.obj().put("session_id", session).put("next_after_id", rows.isEmpty() ? after : rows.get(rows.size() - 1).path("correction_id").asLong()).set("corrections", rows);
    }
    private static void validatePage(long after, int limit) {
        if (after < -1 || limit < 1 || limit > 200) throw new ApiException(400, "invalid_pagination", "Use a cursor >= -1 and limit 1–200.");
    }

    synchronized ObjectNode correct(String session, JsonNode request) {
        store.session(session);
        String segmentId = Json.id(request, "segment_id"), actual = Json.id(request, "actual_speaker");
        if (store.profiles(session).stream().noneMatch(p -> p.id().equals(actual))) throw new ApiException(404, "speaker_not_found", "Speaker is not enrolled in this session.");
        return store.transaction(() -> {
            try (var q = store.prepare("SELECT body,embedding,eligible FROM segments WHERE session_id=? AND id=?", session, segmentId); var r = q.executeQuery()) {
                if (!r.next()) throw new ApiException(404, "segment_not_found", "Segment is not in this session.");
                ObjectNode body = (ObjectNode) Json.parse(r.getString(1)); String embedding = r.getString(2); boolean eligible = r.getBoolean(3);
                String previous = body.path("speaker_id").isNull() ? null : body.path("speaker_id").asText();
                if (actual.equals(previous) && body.path("source").asText().equals("human_correction"))
                    return Json.obj().put("correction_logged", false).put("already_applied", true).put("profile_updated", false).set("attribution", body);
                store.execute("INSERT INTO corrections(session_id,segment_id,previous_speaker,actual_speaker,created_ms,profile_updated) VALUES(?,?,?,?,?,?)",
                    session, segmentId, previous, actual, clock.millis(), eligible);
                if (eligible) {
                    store.execute("INSERT INTO correction_examples VALUES(?,?,?,?) ON CONFLICT(segment_id) DO UPDATE SET speaker_id=excluded.speaker_id,embedding=excluded.embedding", segmentId, session, actual, embedding);
                    store.rebuildProfiles(session);
                }
                // Preserve model score and original identity; a human label does not calibrate the model.
                if (!body.has("original_confidence")) body.set("original_confidence", body.get("confidence"));
                body.put("speaker_id", actual).put("source", "human_correction").put("corrected_at_ms", clock.millis())
                    .putNull("confidence").put("confidence_kind", "human_label").put("trusted", false).put("uncertain", true);
                store.execute("UPDATE segments SET speaker_id=?,body=? WHERE id=?", actual, body.toString(), segmentId);
                var response = Json.obj().put("correction_logged", true).put("profile_updated", eligible)
                    .put("profile_update_reason", eligible ? "clean_speech_example" : "segment_not_verified_as_single_speaker");
                return response.set("attribution", body);
            }
        });
    }

    synchronized ObjectNode end(String session) {
        store.session(session);
        store.transaction(() -> { store.execute("UPDATE sessions SET status='ended' WHERE id=?", session); return null; });
        buffers.remove(session); failures.remove(session);
        return Json.obj().put("session_id", session).put("status", "ended");
    }
    synchronized ObjectNode delete(String session) {
        store.session(session);
        store.transaction(() -> { store.execute("DELETE FROM sessions WHERE id=?", session); return null; });
        buffers.remove(session); failures.remove(session);
        return Json.obj().put("session_id", session).put("deleted", true);
    }
    private static String hash(byte[] pcm, String text) {
        try { var digest = MessageDigest.getInstance("SHA-256"); digest.update(pcm); digest.update((text == null ? "" : text).getBytes(java.nio.charset.StandardCharsets.UTF_8)); return HexFormat.of().formatHex(digest.digest()); }
        catch (NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }
}
