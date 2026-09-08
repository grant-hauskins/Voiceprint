package dev.voiceprint;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.sql.SQLException;
import java.time.*;
import java.util.*;

/** All methods run under SpeakerService's operation monitor, including deletion and inference. */
final class PrivacyGate {
    private final Store store;
    private final Clock clock;
    final PrivacyPolicy policy;
    private final SecureRandom random = new SecureRandom();
    PrivacyGate(Store store, Clock clock, PrivacyPolicy policy) { this.store = store; this.clock = clock; this.policy = policy; }
    private ObjectNode room(String session) {
        try (var p = store.prepare("SELECT * FROM privacy_rooms WHERE session_id=?", session); var r = p.executeQuery()) {
            if (!r.next()) throw new ApiException(404, "privacy_room_not_found", "Create a consent room first.");
            return Json.obj().put("session_id", session).put("state", r.getString("state")).put("roster_version", r.getInt("roster_version"))
                .put("retention_deadline_ms", r.getLong("retention_deadline_ms")).put("policy_version", r.getString("policy_version"));
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    ObjectNode rooms() {
        var rows = Json.arr();
        try (var p = store.prepare("SELECT session_id,state FROM privacy_rooms ORDER BY created_ms DESC LIMIT 200"); var r = p.executeQuery()) {
            while (r.next()) rows.add(Json.obj().put("session_id", r.getString(1)).put("state", r.getString(2)));
        } catch (SQLException e) { throw new IllegalStateException(e); }
        return Json.obj().set("rooms", rows);
    }
    ObjectNode create(JsonNode request) {
        policy.requireConfigured();
        String session = Json.id(request, "session_id");
        if (!PrivacyPolicy.PURPOSE.equals(Json.text(request, "purpose_id", 80))) throw new ApiException(400, "invalid_purpose", "Only the current live conversation purpose is supported.");
        var members = request.path("participants");
        if (!members.isArray() || members.size() < 2 || members.size() > 4) throw new ApiException(400, "invalid_participants", "A room requires 2–4 consenting humans.");
        var ids = new HashSet<String>();
        for (var member : members) {
            if (!ids.add(Json.id(member, "id"))) throw new ApiException(400, "duplicate_participant", "Roster IDs must be distinct.");
            Json.text(member, "name", 200); Json.text(member, "contact", 200);
        }
        try { room(session); throw new ApiException(409, "privacy_room_exists", "Room IDs cannot be reused or reactivated."); }
        catch (ApiException e) { if (e.status != 404) throw e; }
        long now = clock.millis();
        return store.transaction(() -> {
            store.execute("INSERT INTO privacy_rooms(session_id,purpose_id,state,created_ms,last_interaction_ms,retention_deadline_ms,policy_version) VALUES(?,?,'pending',?,?,?,?)", session, PrivacyPolicy.PURPOSE, now, now, now + PrivacyPolicy.INACTIVITY_MS, PrivacyPolicy.VERSION);
            for (var member : members) store.execute("INSERT INTO biometric_consents(consent_id,session_id,participant_id,subject_name,subject_contact,operator_identity) VALUES(?,?,?,?,?,?)", UUID.randomUUID().toString(), session, member.path("id").asText(), member.path("name").asText().strip(), member.path("contact").asText().strip(), policy.operatorIdentity());
            audit(session, null, "room_created", Json.obj().put("roster_count", members.size()));
            return Json.obj().put("session_id", session).put("state", "pending");
        });
    }
    ObjectNode status(String session) {
        var room = room(session); var participants = Json.arr();
        boolean valid = policy.configured() && PrivacyPolicy.VERSION.equals(room.path("policy_version").asText())
            && room.path("state").asText().equals("active") && clock.millis() < room.path("retention_deadline_ms").asLong();
        boolean audio = true, hosted = true, negotiation = true;
        try (var p = store.prepare("SELECT * FROM biometric_consents WHERE session_id=? ORDER BY participant_id", session); var r = p.executeQuery()) {
            while (r.next()) {
                boolean granted = r.getBoolean("bipa_consent_granted") && r.getObject("revoked_at_ms") == null
                    && PrivacyPolicy.METHOD.equals(r.getString("consent_method_version")) && policy.noticeHash().equals(r.getString("notice_sha256"))
                    && r.getObject("consent_timestamp") != null && r.getString("subject_name").equals(r.getString("signature_text"));
                var member = Json.obj().put("id", r.getString("participant_id")).put("name", r.getString("subject_name")).put("bipa_consent_granted", granted);
                if (r.getObject("consent_timestamp") == null) member.putNull("consent_timestamp"); else member.put("consent_timestamp", r.getLong("consent_timestamp"));
                participants.add(member); valid &= granted;
                var scopes = Json.parse(r.getString("disclosure_scopes"));
                audio &= contains(scopes, "openai_audio"); hosted &= contains(scopes, "hosted_mcp"); negotiation &= contains(scopes, "negotiation_text");
            }
        } catch (SQLException e) { throw new IllegalStateException(e); }
        valid &= participants.size() >= 2;
        room.put("policy_version", PrivacyPolicy.VERSION).put("consent_method_version", PrivacyPolicy.METHOD).put("allowed", valid);
        room.set("participants", participants);
        room.set("scopes", Json.obj().put("local_processing", valid).put("openai_audio", valid && audio && policy.openaiReviewed())
            .put("hosted_mcp", valid && hosted && policy.openaiReviewed() && policy.cloudflareReviewed())
            .put("negotiation_text", valid && negotiation && policy.openaiReviewed()));
        return room;
    }
    /** Human roster of the room: every biometric_consents participant, whether or not enrollment has happened yet. */
    List<String> roster(String session) {
        var result = new ArrayList<String>();
        try (var p = store.prepare("SELECT participant_id FROM biometric_consents WHERE session_id=? ORDER BY participant_id", session); var r = p.executeQuery()) { while (r.next()) result.add(r.getString(1)); }
        catch (SQLException e) { throw new IllegalStateException(e); }
        return result;
    }
    private static boolean contains(JsonNode array, String value) { for (var item : array) if (item.asText().equals(value)) return true; return false; }
    void require(String session, String scope) {
        try { if (status(session).path("scopes").path(scope).asBoolean(false)) return; }
        catch (ApiException e) { if (e.status != 404) throw e; }
        throw new ApiException(403, "prior_written_release_required", "Current written releases from every human and the required disclosure approvals are needed for this action.");
    }
    private void signable(String session, String participant) {
        policy.requireConfigured();
        var room = room(session);
        if (!Set.of("pending", "active").contains(room.path("state").asText()) || clock.millis() >= room.path("retention_deadline_ms").asLong())
            throw new ApiException(403, "release_rejected", "This room cannot collect a new release.");
        try (var p = store.prepare("SELECT bipa_consent_granted FROM biometric_consents WHERE session_id=? AND participant_id=?", session, participant); var r = p.executeQuery()) {
            if (!r.next() || r.getBoolean(1)) throw new ApiException(403, "release_rejected", "This participant cannot submit a new release in this room.");
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    ObjectNode challenge(String session, String participant) {
        signable(session, participant);
        byte[] bytes = new byte[32]; random.nextBytes(bytes);
        String nonce = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes), hash = PrivacyPolicy.sha256(nonce.getBytes(StandardCharsets.UTF_8));
        long expiry = clock.millis() + 15 * 60 * 1000L;
        store.transaction(() -> {
            store.execute("UPDATE consent_challenges SET consumed=1 WHERE session_id=? AND participant_id=?", session, participant);
            store.execute("INSERT INTO consent_challenges(nonce_sha256,session_id,participant_id,notice_sha256,expires_at_ms) VALUES(?,?,?,?,?)", hash, session, participant, policy.noticeHash(), expiry);
            return null;
        });
        return Json.obj().put("challenge", nonce).put("expires_at_ms", expiry).put("notice_sha256", policy.noticeHash());
    }
    ObjectNode release(String session, String participant, JsonNode request) {
        signable(session, participant);
        if (!request.path("accepted").isBoolean() || !request.path("accepted").asBoolean()) throw new ApiException(403, "release_rejected", "The participant must personally affirm the written release.");
        String nonce, signature, notice;
        try { nonce = Json.text(request, "challenge", 200); signature = Json.text(request, "signature_text", 200).strip(); notice = Json.text(request, "notice_sha256", 64); }
        catch (ApiException e) { throw new ApiException(403, "release_rejected", "The release fields are incomplete."); }
        var scopes = request.path("disclosure_scopes");
        if (!scopes.isArray()) throw new ApiException(403, "release_rejected", "Select disclosure scopes explicitly, or use an empty list.");
        var unique = new HashSet<String>();
        for (var scope : scopes) if (!scope.isTextual() || !Set.of("openai_audio", "hosted_mcp", "negotiation_text").contains(scope.asText()) || !unique.add(scope.asText())) throw new ApiException(403, "release_rejected", "Invalid disclosure scope.");
        if (!notice.equals(policy.noticeHash())) throw new ApiException(403, "release_rejected", "Review the current notice before signing.");
        return store.transaction(() -> {
            long now = clock.millis(); String consentId;
            try (var p = store.prepare("SELECT consent_id,subject_name FROM biometric_consents WHERE session_id=? AND participant_id=?", session, participant); var r = p.executeQuery()) {
                if (!r.next() || !signature.equals(r.getString(2))) throw new ApiException(403, "release_rejected", "The typed signature must match the roster name.");
                consentId = r.getString(1);
            }
            String hash = PrivacyPolicy.sha256(nonce.getBytes(StandardCharsets.UTF_8));
            try (var p = store.prepare("UPDATE consent_challenges SET consumed=1 WHERE nonce_sha256=? AND session_id=? AND participant_id=? AND notice_sha256=? AND consumed=0 AND expires_at_ms>?", hash, session, participant, notice, now)) {
                if (p.executeUpdate() != 1) throw new ApiException(403, "release_rejected", "The release challenge is expired, used or does not match this participant.");
            }
            store.execute("UPDATE biometric_consents SET bipa_consent_granted=1,consent_timestamp=?,consent_method_version=?,notice_sha256=?,signature_text=?,identity_method='operator_assisted_unverified_contact',operator_identity=?,disclosure_scopes=?,notice_text=?,last_interaction_ms=?,retention_deadline_ms=? WHERE consent_id=?",
                now, PrivacyPolicy.METHOD, notice, signature, policy.operatorIdentity(), scopes.toString(), policy.noticeText(), now, deadline(now), consentId);
            store.execute("UPDATE privacy_rooms SET state='active' WHERE session_id=? AND NOT EXISTS(SELECT 1 FROM biometric_consents WHERE session_id=? AND bipa_consent_granted=0)", session, session);
            audit(session, participant, "written_release", Json.obj().put("consent_id", consentId).put("method", PrivacyPolicy.METHOD).put("notice_sha256", notice));
            return Json.obj().put("consent_id", consentId).put("consent_timestamp", now).put("consent_method_version", PrivacyPolicy.METHOD);
        });
    }
    void validateRoster(String session, JsonNode roster) {
        require(session, "local_processing");
        var approved = status(session).path("participants");
        if (!roster.isArray() || roster.size() != approved.size()) throw new ApiException(403, "roster_mismatch", "Enrollment must exactly match the released human roster.");
        var seen = new HashSet<String>();
        for (var member : roster) {
            boolean match = false;
            for (var person : approved) if (member.path("id").asText().equals(person.path("id").asText()) && member.path("name").asText().equals(person.path("name").asText())) match = true;
            if (!match || !seen.add(member.path("id").asText())) throw new ApiException(403, "roster_mismatch", "Enrollment must exactly match the released human roster.");
        }
    }
    void linkOpening(String session, String participant, byte[] pcm) throws SQLException {
        require(session, "local_processing");
        store.execute("UPDATE biometric_consents SET opening_audio_sha256=? WHERE session_id=? AND participant_id=? AND bipa_consent_granted=1", PrivacyPolicy.sha256(pcm), session, participant);
    }
    static long deadline(long interaction) {
        long threeYears = Instant.ofEpochMilli(interaction).atZone(ZoneOffset.UTC).plusYears(3).toInstant().toEpochMilli();
        return Math.min(threeYears, interaction + PrivacyPolicy.INACTIVITY_MS);
    }
    void humanInteraction(String session, String participant) throws SQLException {
        if (participant == null) return;
        long now = clock.millis();
        store.execute("UPDATE biometric_consents SET last_interaction_ms=?,retention_deadline_ms=? WHERE session_id=? AND participant_id=? AND bipa_consent_granted=1 AND retention_deadline_ms>?", now, deadline(now), session, participant, now);
        store.execute("UPDATE privacy_rooms SET last_interaction_ms=?,retention_deadline_ms=(SELECT min(retention_deadline_ms) FROM biometric_consents WHERE session_id=?) WHERE session_id=? AND state='active' AND retention_deadline_ms>?", now, session, session, now);
    }
    void markHosted(String session) throws SQLException { store.execute("UPDATE privacy_rooms SET hosted_disclosed=1 WHERE session_id=?", session); }
    ObjectNode revoke(String session, String participant) {
        room(session);
        try (var p = store.prepare("SELECT 1 FROM biometric_consents WHERE session_id=? AND participant_id=?", session, participant); var r = p.executeQuery()) {
            if (!r.next()) throw new ApiException(404, "participant_not_found", "Participant is not in this privacy room.");
        } catch (SQLException e) { throw new IllegalStateException(e); }
        schedule(session, "withdrawal", participant);
        return Json.obj().put("revoked", true).put("state", "destroying");
    }
    void schedule(String session, String reason, String participant) {
        var r = room(session); String state = r.path("state").asText();
        if (state.equals("legacy_blocked")) throw new ApiException(403, "legacy_disposition_required", "Legacy evidence requires an explicit reviewed disposition; it is not silently destroyed.");
        if (state.equals("destroyed") || state.equals("destroying")) return;
        store.transaction(() -> {
            long now = clock.millis();
            store.execute("UPDATE privacy_rooms SET state='destroying',roster_version=roster_version+1,purpose_completed_ms=? WHERE session_id=?", now, session);
            store.execute("UPDATE biometric_consents SET bipa_consent_granted=0,revoked_at_ms=COALESCE(revoked_at_ms,?) WHERE session_id=?", now, session);
            store.execute("UPDATE consent_challenges SET consumed=1 WHERE session_id=?", session);
            // A withdrawal or explicit deletion takes the retained summary with it; purpose completion and expiry are the retention path.
            if (reason.equals("withdrawal") || reason.equals("deletion_requested")) store.deleteSummary(session);
            store.execute("INSERT OR IGNORE INTO destruction_jobs(session_id,state,created_ms) VALUES(?,'pending',?)", session, now);
            long job;
            try (var p = store.prepare("SELECT job_id FROM destruction_jobs WHERE session_id=?", session); var row = p.executeQuery()) { row.next(); job = row.getLong(1); }
            store.execute("INSERT OR IGNORE INTO destruction_items(job_id,destination,state) VALUES(?,'sqlite_session_graph','pending')", job);
            try (var p = store.prepare("SELECT hosted_disclosed FROM privacy_rooms WHERE session_id=?", session); var row = p.executeQuery()) {
                if (row.getBoolean(1)) store.execute("INSERT OR IGNORE INTO destruction_items(job_id,destination,state,last_error) VALUES(?,'vendor_deletion_evidence','pending','Provider deletion evidence has not been verified.')", job);
            }
            audit(session, participant, reason, Json.obj().put("job_id", job));
            return null;
        });
    }
    List<String> expiredRooms() {
        var rows = new ArrayList<String>();
        try (var p = store.prepare("SELECT session_id FROM privacy_rooms WHERE state IN ('active','pending') AND retention_deadline_ms<=? LIMIT 100", clock.millis()); var r = p.executeQuery()) { while (r.next()) rows.add(r.getString(1)); }
        catch (SQLException e) { throw new IllegalStateException(e); }
        return rows;
    }
    void sweep() {
        for (String session : expiredRooms()) schedule(session, "deadline_expired", null);
        // Summaries are a separately retained artifact with their own 30-day deadline, not part of the session graph.
        try { store.deleteExpiredSummaries(clock.millis()); } catch (SQLException e) { throw new IllegalStateException(e); }
        var jobs = new ArrayList<Long>();
        try (var p = store.prepare("SELECT job_id FROM destruction_jobs WHERE state!='complete' AND (lease_until_ms IS NULL OR lease_until_ms<=?) ORDER BY job_id LIMIT 100", clock.millis()); var r = p.executeQuery()) { while (r.next()) jobs.add(r.getLong(1)); }
        catch (SQLException e) { throw new IllegalStateException(e); }
        for (long job : jobs) purge(job);
    }
    private void purge(long job) {
        String session;
        try (var p = store.prepare("SELECT session_id FROM destruction_jobs WHERE job_id=?", job); var r = p.executeQuery()) { r.next(); session = r.getString(1); }
        catch (SQLException e) { throw new IllegalStateException(e); }
        try {
            store.execute("UPDATE destruction_jobs SET state='running',attempts=attempts+1,lease_until_ms=? WHERE job_id=?", clock.millis() + 60000, job);
            boolean sqliteDone;
            try (var p = store.prepare("SELECT state FROM destruction_items WHERE job_id=? AND destination='sqlite_session_graph'", job); var r = p.executeQuery()) { sqliteDone = r.getString(1).equals("verified"); }
            if (!sqliteDone) {
                store.transaction(() -> {
                    store.execute("DELETE FROM sessions WHERE id=?", session);
                    // These reference the privacy room rather than the session, so no cascade reaches them.
                    for (String table : List.of("objectives", "agent_channel", "channel_reveals")) store.execute("DELETE FROM " + table + " WHERE session_id=?", session);
                    store.execute("UPDATE biometric_consents SET opening_audio_sha256=NULL WHERE session_id=?", session);
                    store.execute("DELETE FROM consent_challenges WHERE session_id=?", session);
                    store.execute("UPDATE destruction_items SET attempts=attempts+1 WHERE job_id=? AND destination='sqlite_session_graph'", job);
                    return null;
                });
                try (var p = store.prepare("PRAGMA wal_checkpoint(TRUNCATE)"); var r = p.executeQuery()) { if (r.getInt(1) != 0) throw new SQLException("WAL checkpoint busy"); }
                store.execute("VACUUM");
                try (var p = store.prepare("PRAGMA wal_checkpoint(TRUNCATE)"); var r = p.executeQuery()) { if (r.getInt(1) != 0) throw new SQLException("WAL checkpoint busy after compaction"); }
                for (String table : List.of("sessions", "profiles", "participants", "segments", "corrections", "correction_examples", "utterances", "events", "floor", "mcp_calls", "objectives", "agent_channel", "channel_reveals")) {
                    String column = table.equals("sessions") ? "id" : "session_id";
                    try (var p = store.prepare("SELECT count(*) FROM " + table + " WHERE " + column + "=?", session); var r = p.executeQuery()) { if (r.getLong(1) != 0) throw new SQLException("Session graph remains"); }
                }
                store.execute("UPDATE destruction_items SET state='verified',receipt=?,last_error=NULL WHERE job_id=? AND destination='sqlite_session_graph'", Json.obj().put("verified_at_ms", clock.millis()).put("rows_remaining", 0).put("secure_delete", true).put("wal_checkpoint", "truncated").put("compaction", "complete").toString(), job);
            }
            boolean complete;
            try (var p = store.prepare("SELECT count(*) FROM destruction_items WHERE job_id=? AND state!='verified'", job); var r = p.executeQuery()) { complete = r.getInt(1) == 0; }
            store.transaction(() -> {
                store.execute("UPDATE destruction_jobs SET state=?,lease_until_ms=NULL,last_error=? WHERE job_id=?", complete ? "complete" : "pending", complete ? null : "Deletion evidence is pending for an external destination.", job);
                if (complete) store.execute("UPDATE privacy_rooms SET state='destroyed' WHERE session_id=?", session);
                return null;
            });
        } catch (Exception e) {
            try {
                store.execute("UPDATE destruction_jobs SET state='pending',lease_until_ms=NULL,last_error=? WHERE job_id=?", "Purge failed: " + e.getClass().getSimpleName(), job);
                store.execute("UPDATE destruction_items SET state='failed',last_error=? WHERE job_id=? AND destination='sqlite_session_graph' AND state!='verified'", "Purge failed: " + e.getClass().getSimpleName(), job);
            } catch (SQLException failure) { throw new IllegalStateException(failure); }
        }
    }
    ObjectNode destruction(String session) {
        var room = room(session); var jobs = Json.arr(); var items = Json.arr(); var failures = Json.arr();
        try (var p = store.prepare("SELECT job_id,state,last_error,attempts FROM destruction_jobs WHERE session_id=?", session); var r = p.executeQuery()) {
            while (r.next()) {
                long id = r.getLong(1); jobs.add(Json.obj().put("job_id", id).put("state", r.getString(2)).put("attempts", r.getInt(4)));
                if (r.getString(3) != null) failures.add(r.getString(3));
                try (var q = store.prepare("SELECT destination,state,last_error FROM destruction_items WHERE job_id=?", id); var row = q.executeQuery()) {
                    while (row.next()) { items.add(Json.obj().put("destination", row.getString(1)).put("state", row.getString(2))); if (row.getString(3) != null) failures.add(row.getString(3)); }
                }
            }
        } catch (SQLException e) { throw new IllegalStateException(e); }
        var result = Json.obj().put("session_id", session).put("state", room.path("state").asText()).put("verified", room.path("state").asText().equals("destroyed"));
        result.set("jobs", jobs); result.set("items", items); result.set("failures", failures); return result;
    }
    private void audit(String session, String participant, String action, JsonNode details) throws SQLException {
        store.execute("INSERT INTO consent_audit(session_id,participant_id,timestamp_ms,action,detail) VALUES(?,?,?,?,?)", session, participant, clock.millis(), action, details.toString());
    }
}
