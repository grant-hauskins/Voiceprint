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
    private final PrivacyGate privacy;
    private Runnable destructionWakeup = () -> {};
    private final Map<String, byte[]> buffers = new HashMap<>();
    private final Map<String, String> failures = new HashMap<>();
    SpeakerService(Store store, SpeechEngine engine, Clock clock) { this(store, engine, clock, PrivacyPolicy.environment()); }
    SpeakerService(Store store, SpeechEngine engine, Clock clock, PrivacyPolicy policy) {
        this.store = store; this.engine = engine; this.clock = clock; this.privacy = new PrivacyGate(store, clock, policy);
    }
    synchronized ObjectNode privacyNotice() { return privacy.policy.notice(); }
    synchronized ObjectNode privacyRooms() { return privacy.rooms(); }
    synchronized ObjectNode createPrivacyRoom(JsonNode request) { return privacy.create(request); }
    synchronized ObjectNode consentStatus(String session) { return privacy.status(session); }
    synchronized ObjectNode consentChallenge(String session, String participant) { return privacy.challenge(session, participant); }
    synchronized ObjectNode consentRelease(String session, String participant, JsonNode request) { return privacy.release(session, participant, request); }
    synchronized ObjectNode consentRevoke(String session, String participant) {
        var result = privacy.revoke(session, participant);
        buffers.remove(session); failures.remove(session); notifyAll(); destructionWakeup.run(); return result;
    }
    synchronized ObjectNode destructionStatus(String session) { return privacy.destruction(session); }
    synchronized void requireConsent(String session, String scope) { privacy.require(session, scope); }
    synchronized void setDestructionWakeup(Runnable wakeup) { destructionWakeup = wakeup; }
    synchronized void sweepPrivacy() {
        for (String session : privacy.expiredRooms()) { buffers.remove(session); failures.remove(session); }
        privacy.sweep(); notifyAll();
    }
    synchronized void authorizeHosted(String tool, JsonNode arguments) {
        if (!privacy.policy.openaiReviewed() || !privacy.policy.cloudflareReviewed()) throw new ApiException(403, "vendor_review_required", "Hosted MCP requires reviewed OpenAI and Cloudflare agreements and settings.");
        if (tool.equals("list_sessions")) {
            for (var row : sessions(200, true).path("sessions")) markHosted(row.path("session_id").asText());
        } else { String session = Json.id(arguments, "session_id"); privacy.require(session, "hosted_mcp"); markHosted(session); }
    }
    private void markHosted(String session) { store.transaction(() -> { privacy.markHosted(session); return null; }); }

    synchronized ObjectNode init(JsonNode request) {
        String session = Json.id(request, "session_id");
        privacy.require(session, "local_processing");
        if (Json.integer(request, "sample_rate", 16000, 16000) != 16000 || !"pcm_s16le".equals(Json.text(request, "audio_format", 20)))
            throw new ApiException(400, "invalid_audio_format", "Expected mono 16 kHz pcm_s16le.");
        JsonNode participants = request.path("participants");
        if (!participants.isArray() || participants.size() < 2 || participants.size() > 4)
            throw new ApiException(400, "invalid_participants", "Enroll 2–4 participants.");
        privacy.validateRoster(session, participants);
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
            privacy.require(session, "local_processing");
            var result = engine.analyze(pcm, true);
            if (!result.speech() || result.embedding() == null || result.overlap().equals("detected") || result.speakerChange())
                throw new ApiException(422, "invalid_enrollment", "Each opening statement must contain one participant speaking clearly.");
            if (!results.isEmpty() && (!results.getFirst().modelId().equals(result.modelId()) || results.getFirst().embedding().length != result.embedding().length))
                throw new ApiException(503, "model_mismatch", "The speech model changed during enrollment.");
            results.add(result);
        }
        privacy.require(session, "local_processing");
        return store.transaction(() -> {
            store.execute("INSERT INTO sessions(id,status,created_ms) VALUES(?,'active',?)", session, clock.millis());
            for (int i = 0; i < participants.size(); i++) {
                var p = participants.get(i); var result = results.get(i); String vector = Store.vectorJson(Audio.normalize(result.embedding()));
                store.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?)", session, p.get("id").asText(), p.get("name").asText(), result.modelId(), vector, vector);
                store.execute("INSERT INTO participants VALUES(?,?,?,'human',NULL,?)", session, p.get("id").asText(), p.get("name").asText(), result.modelId());
                privacy.linkOpening(session, p.get("id").asText(), audio.get(i));
            }
            return Json.obj().put("session_id", session).put("profiles_created", participants.size()).put("status", "ready")
                .put("confidence_kind", "uncalibrated").put("chunk_ms", 250).put("context_ms", 1500);
        });
    }

    synchronized ObjectNode ingest(String session, JsonNode request) {
        privacy.require(session, "local_processing");
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
        privacy.require(session, "local_processing");
        SpeechEngine.Result completed = result;
        store.transaction(() -> {
            boolean eligible = completed != null && completed.embedding() != null && completed.speech() && completed.profileEligible()
                && completed.overlap().equals("clear") && !completed.speakerChange();
            store.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,?)", attribution.get("segment_id").asText(), session, sequence, hash,
                attribution.path("speaker_id").isNull() ? null : attribution.path("speaker_id").asText(), attribution.toString(),
                completed == null || completed.embedding() == null ? null : Store.vectorJson(completed.embedding()), eligible);
            store.execute("UPDATE sessions SET next_sequence=next_sequence+1,elapsed_ms=elapsed_ms+250 WHERE id=?", session);
            if (completed != null && completed.speech() && !attribution.path("speaker_id").isNull()) privacy.humanInteraction(session, attribution.path("speaker_id").asText());
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
        privacy.require(session, "local_processing");
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
        privacy.require(session, "local_processing");
        store.session(session); validatePage(after, limit);
        if (speaker != null && store.profiles(session).stream().noneMatch(p -> p.id().equals(speaker))) throw new ApiException(404, "speaker_not_found", "Speaker is not enrolled in this session.");
        ArrayNode rows = store.transcript(session, speaker, after, limit);
        return Json.obj().put("session_id", session).put("next_after_sequence", rows.isEmpty() ? after : rows.get(rows.size() - 1).path("sequence").asLong()).set("transcript", rows);
    }
    synchronized ObjectNode profiles(String session) {
        privacy.require(session, "local_processing");
        store.session(session); var profiles = Json.arr();
        for (var p : store.profiles(session)) profiles.add(Json.obj().put("id", p.id()).put("name", p.name()).put("model_id", p.model()));
        return Json.obj().put("session_id", session).set("participants", profiles);
    }
    record Registration(boolean created, ObjectNode response) {}
    synchronized Registration register(String session, JsonNode request) {
        privacy.require(session, "local_processing");
        if (!store.session(session).status().equals("active")) throw new ApiException(409, "session_ended", "This session has ended.");
        String id = Json.id(request, "id"), name = Json.text(request, "name", 200);
        if (!"agent".equals(Json.text(request, "kind", 20))) throw new ApiException(400, "invalid_participant", "Register agents here; humans require voice enrollment.");
        String provider = Json.text(request, "provider", 200), model = Json.text(request, "model", 200);
        var participant = Json.obj().put("id", id).put("name", name).put("kind", "agent").put("provider", provider).put("model", model);
        ObjectNode response = Json.obj().put("session_id", session).set("participant", participant);
        for (var existing : store.participants(session)) {
            if (!existing.path("id").asText().equals(id)) continue;
            if (!existing.equals(participant)) throw new ApiException(409, "participant_exists", "Participant ID already has different registration fields.");
            return new Registration(false, response);
        }
        store.transaction(() -> { store.execute("INSERT INTO participants VALUES(?,?,?,'agent',?,?)", session, id, name, provider, model); return null; });
        return new Registration(true, response);
    }
    synchronized ObjectNode participants(String session) {
        privacy.require(session, "local_processing");
        store.session(session);
        return Json.obj().put("session_id", session).set("participants", store.participants(session));
    }
    private static ObjectNode floorData(Store.Lease lease, long now) {
        var result = Json.obj().put("server_time_ms", now);
        if (lease == null) return result.putNull("held_by").putNull("expires_at_ms");
        return result.put("held_by", lease.participant()).put("expires_at_ms", lease.expiresAt());
    }
    /** Called only inside a transaction; expiry and its evidence are one durable change. */
    private Store.Lease activeLease(String session, long now) throws java.sql.SQLException {
        var lease = store.lease(session);
        if (lease != null && now >= lease.expiresAt()) {
            store.execute("DELETE FROM floor WHERE session_id=?", session);
            store.event(session, now, "floor", floorData(null, now).put("action", "expired"));
            return null;
        }
        return lease;
    }
    synchronized ObjectNode claimFloor(String session, JsonNode request) {
        privacy.require(session, "local_processing");
        if (!store.session(session).status().equals("active")) throw new ApiException(409, "session_ended", "This session has ended.");
        String participant = Json.id(request, "participant_id");
        if (!store.participant(session, participant).path("kind").asText().equals("agent")) throw new ApiException(400, "invalid_participant", "Only registered agents can claim the floor.");
        long duration = request.has("lease_ms") ? Json.integer(request, "lease_ms", 1000, 30000) : 15000;
        ObjectNode result = store.transaction(() -> {
            long now = clock.millis();
            var previous = activeLease(session, now);
            boolean granted = previous == null || previous.participant().equals(participant);
            var lease = granted ? new Store.Lease(participant, now + duration) : previous;
            if (granted) {
                store.execute("INSERT INTO floor VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET participant_id=excluded.participant_id,expires_at_ms=excluded.expires_at_ms", session, participant, lease.expiresAt());
                store.event(session, now, "floor", floorData(lease, now).put("action", previous == null ? "granted" : "renewed"));
            }
            return floorData(lease, now).put("session_id", session).put("granted", granted);
        });
        notifyAll();
        return result;
    }
    synchronized ObjectNode floor(String session) {
        privacy.require(session, "local_processing");
        store.session(session);
        ObjectNode result = store.transaction(() -> {
            long now = clock.millis();
            return floorData(activeLease(session, now), now).put("session_id", session);
        });
        notifyAll();
        return result;
    }
    synchronized ObjectNode releaseFloor(String session, String participant) {
        privacy.require(session, "local_processing");
        store.session(session);
        Json.id(Json.obj().put("participant_id", participant), "participant_id");
        ObjectNode result = store.transaction(() -> {
            long now = clock.millis();
            var lease = activeLease(session, now);
            boolean released = lease != null && lease.participant().equals(participant);
            if (released) {
                store.execute("DELETE FROM floor WHERE session_id=?", session);
                store.event(session, now, "floor", floorData(null, now).put("action", "released"));
                lease = null;
            }
            return floorData(lease, now).put("session_id", session).put("released", released);
        });
        notifyAll();
        return result;
    }
    synchronized ObjectNode events(String session, long after, int limit, long waitMs) {
        if (after < 0 || limit < 1 || limit > 200 || waitMs < 0 || waitMs > 10000)
            throw new ApiException(400, "invalid_pagination", "Use after_id >= 0, limit 1–200, and wait_ms 0–10000.");
        long deadline = System.nanoTime() + waitMs * 1_000_000;
        while (true) {
            privacy.require(session, "local_processing");
            store.session(session);
            var lease = store.transaction(() -> activeLease(session, clock.millis()));
            var rows = store.events(session, after, limit);
            long remaining = (deadline - System.nanoTime()) / 1_000_000;
            if (!rows.isEmpty() || remaining <= 0 || Thread.currentThread().isInterrupted())
                return Json.obj().put("session_id", session).put("next_after_id", rows.isEmpty() ? after : rows.get(rows.size() - 1).path("event_id").asLong()).set("events", rows);
            long pause = lease == null ? remaining : Math.min(remaining, Math.max(1, lease.expiresAt() - clock.millis()));
            // Object.wait releases the service monitor: audio, floor and MCP writes can proceed.
            try { wait(Math.max(1, pause)); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }
    }
    /** Internal HTTP MCP transport hook; no HTTP write endpoint and no headers or credentials. */
    synchronized void recordMcpCall(String caller, String participantTag, String tool, JsonNode arguments, int bytes, boolean failed) {
        String requestedSession = arguments.path("session_id").isTextual() ? arguments.path("session_id").asText() : null;
        String session = null, participant = null;
        if (requestedSession != null) {
            try { privacy.require(requestedSession, "local_processing"); store.session(requestedSession); session = requestedSession; }
            catch (ApiException e) { if (e.status != 404 && e.status != 403) throw e; }
        }
        if (session != null && participantTag != null) {
            try { if (store.participant(session, participantTag).path("kind").asText().equals("agent")) participant = participantTag; }
            catch (ApiException e) { if (e.status != 404) throw e; }
        }
        String recordedSession = session, recordedParticipant = participant;
        ObjectNode safeArguments = safeMcpArguments(arguments);
        store.transaction(() -> {
            long now = clock.millis();
            store.execute("INSERT INTO mcp_calls(session_id,timestamp_ms,caller_ip,participant_id,tool,arguments,bytes,failed) VALUES(?,?,?,?,?,?,?,?)", recordedSession, now, caller, recordedParticipant, tool, safeArguments.toString(), bytes, failed);
            long id = store.lastInsertId();
            var data = Json.obj().put("call_id", id).put("session_id", recordedSession).put("timestamp_ms", now).put("caller_ip", caller).put("participant_id", recordedParticipant)
                .put("tool", tool).put("bytes", bytes).put("failed", failed).set("arguments", safeArguments);
            if (recordedSession != null) store.event(recordedSession, now, "mcp_call", data);
            return null;
        });
        notifyAll();
    }
    static ObjectNode safeMcpArguments(JsonNode arguments) {
        var safe = Json.obj();
        for (String key : List.of("session_id", "speaker_id", "segment_id", "actual_speaker", "text")) if (arguments.has(key)) safe.put(key, "[redacted]");
        for (String key : List.of("limit", "after_id", "after_sequence")) if (arguments.path(key).isIntegralNumber()) safe.set(key, arguments.path(key));
        if (Set.of("high", "medium", "low").contains(arguments.path("min_label").asText())) safe.put("min_label", arguments.path("min_label").asText());
        if (Set.of("board", "raw").contains(arguments.path("tier").asText())) safe.put("tier", arguments.path("tier").asText());
        if (TAGS.contains(arguments.path("tag").asText())) safe.put("tag", arguments.path("tag").asText());
        return safe;
    }
    static final Set<String> TAGS = Set.of("OBJECTIVE_ACHIEVED", "REFOCUS_NEEDED");

    // ---- V3: objectives, agent channel, reveal gate and summary (docs/API.md, V3 arbitration contract) ----

    private String principal(String session, JsonNode request, String key) {
        String id = Json.id(request, key);
        if (!privacy.roster(session).contains(id)) throw new ApiException(404, "participant_not_found", "Participant is not a human in this room's released roster.");
        return id;
    }
    /** Every POST is a new version; constraint values are what the redaction guard watches. */
    synchronized ObjectNode createObjective(String session, JsonNode request) {
        privacy.require(session, "local_processing"); privacy.require(session, "negotiation_text");
        String principal = principal(session, request, "principal_id");
        String position = Json.text(request, "position", 2000), trigger = Json.text(request, "trigger", 200);
        String source = Json.text(request, "source", 20);
        if (!Set.of("typed", "uploaded").contains(source)) throw new ApiException(400, "invalid_input", "source must be typed or uploaded.");
        JsonNode given = request.path("constraints");
        if (!given.isArray() || given.size() > 20) throw new ApiException(400, "invalid_input", "constraints must be an array of at most 20 items.");
        var constraints = Json.arr();
        for (JsonNode c : given) constraints.add(Json.obj().put("label", Json.text(c, "label", 80)).put("value", Json.text(c, "value", 200)));
        return store.transaction(() -> {
            long now = clock.millis();
            long version = store.insertObjective(session, principal, position, constraints.toString(), source, trigger, now);
            return Json.obj().put("session_id", session).put("principal_id", principal).put("version", version).put("created_ms", now);
        });
    }
    synchronized ObjectNode objectives(String session, boolean history) {
        privacy.require(session, "local_processing"); privacy.require(session, "negotiation_text");
        return Json.obj().put("session_id", session).set("objectives", store.objectives(session, history));
    }
    /** The guard runs on the write path against every latest objective's values; the row is stored already redacted. */
    synchronized ObjectNode postChannel(String session, JsonNode request) {
        privacy.require(session, "local_processing");
        if (!store.session(session).status().equals("active")) throw new ApiException(409, "session_ended", "This session has ended.");
        String sender = Json.id(request, "sender_participant_id");
        if (!store.participant(session, sender).path("kind").asText().equals("agent")) throw new ApiException(400, "invalid_participant", "Only registered agents post to the agent channel.");
        String tier = Json.text(request, "tier", 10);
        if (!Set.of("board", "raw").contains(tier)) throw new ApiException(400, "invalid_input", "tier must be board or raw.");
        String tag = null;
        if (request.has("tag") && !request.get("tag").isNull()) {
            tag = Json.text(request, "tag", 40);
            if (!TAGS.contains(tag)) throw new ApiException(400, "invalid_input", "tag must be OBJECTIVE_ACHIEVED or REFOCUS_NEEDED.");
        }
        var guarded = Redaction.redact(Json.text(request, "text", 4000), store.constraintValues(session));
        String stored = tag;
        ObjectNode result = store.transaction(() -> {
            long now = clock.millis();
            long id = store.insertChannel(session, sender, tier, stored, guarded.text(), guarded.hits(), now);
            if (tier.equals("board")) store.event(session, now, "agent_channel", store.channelRow(session, id));
            return Json.obj().put("session_id", session).put("row_id", id).put("tier", tier).put("redactions", guarded.hits()).put("text", guarded.text());
        });
        notifyAll();
        return result;
    }
    private boolean revealed(String session) {
        var reveals = store.reveals(session);
        var roster = privacy.roster(session);
        return !roster.isEmpty() && reveals.containsAll(roster);
    }
    /** Reveal gate: the hosted MCP path sees every tier; anyone else sees raw rows only once every human has revealed. */
    synchronized ObjectNode channel(String session, long after, int limit, String tier, boolean hosted) {
        privacy.require(session, "local_processing");
        store.session(session); validatePage(after, limit);
        if (tier == null || tier.equals("all")) tier = null;
        else if (!Set.of("board", "raw").contains(tier)) throw new ApiException(400, "invalid_query", "tier must be board, raw or all.");
        boolean revealed = revealed(session);
        ArrayNode fetched = store.channel(session, after, limit, tier);
        long next = fetched.isEmpty() ? after : fetched.get(fetched.size() - 1).path("row_id").asLong();
        var rows = Json.arr(); var text = new StringBuilder();
        for (var row : fetched) {
            if (!hosted && !revealed && row.path("tier").asText().equals("raw")) continue;
            rows.add(row);
            var time = java.time.LocalTime.ofInstant(java.time.Instant.ofEpochMilli(row.path("timestamp_ms").asLong()), java.time.ZoneId.systemDefault());
            String who = row.path("sender_name").isNull() ? row.path("sender_participant_id").asText() : row.path("sender_name").asText();
            text.append('#').append(row.path("row_id").asLong()).append(' ').append(String.format("%02d:%02d:%02d", time.getHour(), time.getMinute(), time.getSecond()))
                .append(' ').append(who).append(" [").append(row.path("tier").asText()).append(']');
            if (!row.path("tag").isNull()) text.append('(').append(row.path("tag").asText()).append(')');
            text.append(": ").append(row.path("text").asText()).append('\n');
        }
        return Json.obj().put("session_id", session).put("next_after_id", next).put("revealed", revealed).put("text", text.toString()).set("rows", rows);
    }
    synchronized ObjectNode reveal(String session, JsonNode request) {
        privacy.require(session, "local_processing");
        String participant = principal(session, request, "participant_id");
        if (!request.path("revealed").isBoolean()) throw new ApiException(400, "invalid_input", "revealed must be true or false.");
        boolean revealed = request.path("revealed").asBoolean();
        store.transaction(() -> { store.reveal(session, participant, revealed, clock.millis()); return null; });
        var by = Json.arr(); for (String id : store.reveals(session)) by.add(id);
        return Json.obj().put("session_id", session).put("revealed", revealed(session)).set("revealed_by", by);
    }
    /** Written by the runtime before POST .../end; retained on its own 30-day deadline after the room is destroyed. */
    synchronized ObjectNode saveSummary(String session, JsonNode request) {
        privacy.require(session, "local_processing"); privacy.require(session, "negotiation_text");
        if (!store.session(session).status().equals("active")) throw new ApiException(409, "session_ended", "This session has ended.");
        String text = Json.text(request, "text", 20000), model = Json.text(request, "model", 200);
        long board = Json.integer(request, "board_rows", 0, Integer.MAX_VALUE), transcript = Json.integer(request, "transcript_rows", 0, Integer.MAX_VALUE);
        return store.transaction(() -> {
            long now = clock.millis(), deadline = now + PrivacyPolicy.SUMMARY_RETENTION_MS;
            store.saveSummary(session, text, model, board, transcript, now, deadline);
            return Json.obj().put("session_id", session).put("created_ms", now).put("retention_deadline_ms", deadline);
        });
    }
    /** Operator read after the room is gone: no room admission, because destruction has already removed the room's authority. */
    synchronized ObjectNode summary(String session) {
        var row = store.summary(session);
        if (row == null) throw new ApiException(404, "summary_not_found", "No summary is retained for this session.");
        return row;
    }
    synchronized ObjectNode deleteSummary(String session) {
        boolean deleted = store.transaction(() -> store.deleteSummary(session));
        if (!deleted) throw new ApiException(404, "summary_not_found", "No summary is retained for this session.");
        return Json.obj().put("session_id", session).put("deleted", true);
    }
    synchronized ObjectNode corrections(String session, long after, int limit) {
        privacy.require(session, "local_processing");
        store.session(session); validatePage(after, limit);
        ArrayNode rows = store.corrections(session, after, limit);
        return Json.obj().put("session_id", session).put("next_after_id", rows.isEmpty() ? after : rows.get(rows.size() - 1).path("correction_id").asLong()).set("corrections", rows);
    }
    synchronized ObjectNode sessions(int limit) {
        return sessions(limit, false);
    }
    synchronized ObjectNode sessions(int limit, boolean hosted) {
        var allowed = Json.arr();
        for (var row : store.sessions(limit)) {
            try { privacy.require(row.path("session_id").asText(), hosted ? "hosted_mcp" : "local_processing"); allowed.add(row); }
            catch (ApiException ignored) { /* Legacy, withdrawn and unsigned rooms expose no protected metadata. */ }
        }
        return Json.obj().set("sessions", allowed);
    }
    /** Stores one externally transcribed utterance; the API never transcribes audio itself. */
    synchronized ObjectNode utter(String session, JsonNode request) {
        privacy.require(session, "local_processing");
        store.session(session);
        String speaker = null;
        boolean agent = false;
        if (request.has("speaker_id") && !request.get("speaker_id").isNull()) {
            speaker = Json.id(request, "speaker_id");
            agent = store.participant(session, speaker).path("kind").asText().equals("agent");
        }
        long start = Json.integer(request, "start_ms", 0, Long.MAX_VALUE / 2), end = Json.integer(request, "end_ms", 0, Long.MAX_VALUE / 2);
        if (end <= start) throw new ApiException(400, "invalid_input", "end_ms must exceed start_ms.");
        String text = Json.text(request, "text", 4000);
        String source = request.has("source") ? Json.text(request, "source", 40) : "client_asr";
        if (agent != source.equals("agent")) throw new ApiException(400, "invalid_source", "source=agent requires a registered agent speaker and agent speakers require source=agent.");
        Double similarity = agent ? null : ratio(request, "similarity", -1, 1), margin = agent ? null : ratio(request, "margin", -1, 2);
        Double overlapRatio = agent ? null : ratio(request, "overlap_ratio", 0, 1), abstainRatio = agent ? null : ratio(request, "abstain_ratio", 0, 1);
        var candidates = Json.arr();
        if (!agent && request.has("candidates") && request.get("candidates").isArray()) {
            var enrolled = store.profiles(session).stream().map(Store.Profile::id).toList();
            for (JsonNode c : request.get("candidates")) {
                if (!c.isTextual() || !enrolled.contains(c.asText())) throw new ApiException(404, "speaker_not_found", "Candidate is not enrolled in this session.");
                candidates.add(c.asText());
            }
        }
        String label = agent ? "agent" : label(speaker, similarity, margin, overlapRatio, abstainRatio, candidates.size());
        String who = speaker;
        ObjectNode result = store.transaction(() -> {
            long now = clock.millis();
            store.execute("INSERT INTO utterances(session_id,speaker_id,start_ms,end_ms,text,source,created_ms,similarity,margin,overlap_ratio,abstain_ratio,label,candidates) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                session, who, start, end, text, source, now, similarity, margin, overlapRatio, abstainRatio, label, candidates.isEmpty() ? null : candidates.toString());
            long id = store.lastInsertId();
            store.event(session, now, "utterance", store.utterance(session, id));
            if (!source.equals("agent")) privacy.humanInteraction(session, who);
            return Json.obj().put("utterance_id", id).put("session_id", session).put("speaker_id", who).put("start_ms", start).put("end_ms", end).put("label", label).put("stored", true);
        });
        notifyAll();
        return result;
    }
    private static Double ratio(JsonNode n, String key, double min, double max) {
        JsonNode v = n.path(key);
        if (v.isMissingNode() || v.isNull()) return null;
        if (!v.isNumber() || v.doubleValue() < min || v.doubleValue() > max) throw new ApiException(400, "invalid_input", key + " must be a number in [" + min + "," + max + "].");
        return v.doubleValue();
    }
    /**
     * Engineering defaults, not calibrated probabilities: derived from cosine similarity and top-two margin,
     * discounted by overlap and abstention. Replace with the calibration artifact once VP-Live-En-v1 exists.
     */
    static String label(String speaker, Double similarity, Double margin, Double overlapRatio, Double abstainRatio, int candidates) {
        double overlap = overlapRatio == null ? 0 : overlapRatio, abstain = abstainRatio == null ? 0 : abstainRatio;
        if (overlap >= .3 || (speaker == null && candidates > 0)) return "overlap";
        if (speaker == null || similarity == null || margin == null) return "unknown";
        if (abstain >= .5) return "low";
        if (similarity >= .55 && margin >= .25 && overlap < .1 && abstain < .2) return "high";
        if (similarity >= .40 && margin >= .12) return "medium";
        return "low";
    }
    static int labelRank(String minLabel) {
        if (minLabel == null) return 0;
        return switch (minLabel) { case "high" -> 3; case "medium" -> 2; case "low" -> 1; default -> throw new ApiException(400, "invalid_query", "min_label must be high, medium or low."); };
    }
    synchronized ObjectNode utterances(String session, long after, int limit, String minLabel) {
        privacy.require(session, "local_processing");
        store.session(session); validatePage(after, limit);
        ArrayNode rows = store.utterances(session, after, limit, labelRank(minLabel));
        var text = new StringBuilder();
        for (var row : rows) {
            String label = row.path("label").asText();
            String who;
            if (label.equals("overlap")) {
                double ratio = row.path("overlap_ratio").isNull() ? 1 : row.path("overlap_ratio").asDouble();
                String tag = " [overlap " + Math.round(ratio * 100) + "%]";
                if (row.path("candidates").size() > 0) who = "OVERLAP " + store.names(session, row.path("candidates")) + tag;
                else who = (row.path("speaker_name").isNull() ? "unknown" : row.path("speaker_name").asText()) + tag;  // attributed, but partly talked over
            } else {
                String name = row.path("speaker_name").isNull() ? "unknown" : row.path("speaker_name").asText();
                who = name + " [" + label + "]";
            }
            text.append('#').append(row.path("utterance_id").asLong()).append(' ').append(clockText(row.path("start_ms").asLong())).append('-').append(clockText(row.path("end_ms").asLong()))
                .append(' ').append(who).append(": ").append(row.path("text").asText()).append('\n');
        }
        return Json.obj().put("session_id", session).put("next_after_id", rows.isEmpty() ? after : rows.get(rows.size() - 1).path("utterance_id").asLong())
            .put("label_kind", "similarity_based_uncalibrated").put("text", text.toString()).set("utterances", rows);
    }
    private static String clockText(long ms) { return String.format("%d:%02d.%d", ms / 60000, (ms / 1000) % 60, (ms / 100) % 10); }
    private static void validatePage(long after, int limit) {
        if (after < -1 || limit < 1 || limit > 200) throw new ApiException(400, "invalid_pagination", "Use a cursor >= -1 and limit 1–200.");
    }

    synchronized ObjectNode correct(String session, JsonNode request) {
        privacy.require(session, "local_processing");
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
        privacy.schedule(session, "purpose_completed", null);
        buffers.remove(session); failures.remove(session);
        notifyAll(); destructionWakeup.run();
        return Json.obj().put("session_id", session).put("status", "ended").put("state", "destroying");
    }
    synchronized ObjectNode delete(String session) {
        privacy.schedule(session, "deletion_requested", null);
        buffers.remove(session); failures.remove(session);
        notifyAll(); destructionWakeup.run();
        return Json.obj().put("session_id", session).put("deleted", false).put("state", "destroying");
    }
    private static String hash(byte[] pcm, String text) {
        try { var digest = MessageDigest.getInstance("SHA-256"); digest.update(pcm); digest.update((text == null ? "" : text).getBytes(java.nio.charset.StandardCharsets.UTF_8)); return HexFormat.of().formatHex(digest.digest()); }
        catch (NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }
}
