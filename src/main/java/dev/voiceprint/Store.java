package dev.voiceprint;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.*;
import java.nio.file.*;
import java.sql.*;
import java.util.*;

/** One service owns a database. All access is serialized by SpeakerService. */
final class Store implements AutoCloseable {
    final Connection db;
    record Session(String id, String status, long nextSequence, long elapsedMs) {}
    record Profile(String id, String name, String model, double[] anchor, double[] vector) {}
    record Lease(String participant, long expiresAt) {}
    interface Work<T> { T run() throws Exception; }
    Store(Path path) throws Exception {
        Path parent = path.toAbsolutePath().getParent(); Files.createDirectories(parent);
        db = DriverManager.getConnection("jdbc:sqlite:" + path.toAbsolutePath());
        try (var s = db.createStatement()) {
            s.execute("PRAGMA foreign_keys=ON"); s.execute("PRAGMA journal_mode=WAL"); s.execute("PRAGMA busy_timeout=3000");
            s.execute("PRAGMA secure_delete=ON");
            int version; try (var r = s.executeQuery("PRAGMA user_version")) { version = r.getInt(1); }
            if (version > 5) throw new IllegalStateException("Database schema is newer than this application");
            s.execute("CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,status TEXT NOT NULL,created_ms INTEGER NOT NULL,next_sequence INTEGER NOT NULL DEFAULT 0,elapsed_ms INTEGER NOT NULL DEFAULT 0)");
            s.execute("CREATE TABLE IF NOT EXISTS profiles(session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,id TEXT NOT NULL,name TEXT NOT NULL,model TEXT NOT NULL,anchor TEXT NOT NULL,vector TEXT NOT NULL,PRIMARY KEY(session_id,id))");
            s.execute("CREATE TABLE IF NOT EXISTS segments(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,sequence INTEGER NOT NULL,hash TEXT NOT NULL,speaker_id TEXT,body TEXT NOT NULL,embedding TEXT,eligible INTEGER NOT NULL,UNIQUE(session_id,sequence))");
            s.execute("CREATE INDEX IF NOT EXISTS segments_speaker ON segments(session_id,speaker_id,sequence)");
            s.execute("CREATE TABLE IF NOT EXISTS corrections(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,previous_speaker TEXT,actual_speaker TEXT NOT NULL,created_ms INTEGER NOT NULL,profile_updated INTEGER NOT NULL)");
            s.execute("CREATE INDEX IF NOT EXISTS corrections_session ON corrections(session_id,id)");
            s.execute("CREATE TABLE IF NOT EXISTS correction_examples(segment_id TEXT PRIMARY KEY REFERENCES segments(id) ON DELETE CASCADE,session_id TEXT NOT NULL,speaker_id TEXT NOT NULL,embedding TEXT NOT NULL,FOREIGN KEY(session_id,speaker_id) REFERENCES profiles(session_id,id) ON DELETE CASCADE)");
            s.execute("CREATE TABLE IF NOT EXISTS utterances(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,speaker_id TEXT,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,text TEXT NOT NULL,source TEXT NOT NULL,created_ms INTEGER NOT NULL,similarity REAL,margin REAL,overlap_ratio REAL,abstain_ratio REAL,label TEXT NOT NULL DEFAULT 'unknown',candidates TEXT)");
            s.execute("CREATE INDEX IF NOT EXISTS utterances_session ON utterances(session_id,start_ms,id)");
            if (version == 2) { // Upgrade and version the old uncertainty columns atomically before v4.
                db.setAutoCommit(false);
                try {
                    for (String column : new String[] {"similarity REAL", "margin REAL", "overlap_ratio REAL", "abstain_ratio REAL", "label TEXT NOT NULL DEFAULT 'unknown'", "candidates TEXT"})
                        s.execute("ALTER TABLE utterances ADD COLUMN " + column);
                    s.execute("PRAGMA user_version=3");
                    db.commit();
                } catch (Exception e) { db.rollback(); throw e; }
                finally { db.setAutoCommit(true); }
            }
            if (version < 4) {
                // The registry and backfilled event history become visible together, exactly once.
                db.setAutoCommit(false);
                try {
                    s.execute("CREATE TABLE participants(session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,id TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('human','agent')),provider TEXT,model TEXT,PRIMARY KEY(session_id,id))");
                    s.execute("INSERT INTO participants SELECT session_id,id,name,'human',NULL,model FROM profiles");
                    s.execute("CREATE TABLE floor(session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,participant_id TEXT NOT NULL,expires_at_ms INTEGER NOT NULL,FOREIGN KEY(session_id,participant_id) REFERENCES participants(session_id,id) ON DELETE CASCADE)");
                    s.execute("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,timestamp_ms INTEGER NOT NULL,type TEXT NOT NULL,data TEXT NOT NULL)");
                    s.execute("CREATE INDEX events_session ON events(session_id,id)");
                    s.execute("CREATE TABLE mcp_calls(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,timestamp_ms INTEGER NOT NULL,caller_ip TEXT NOT NULL,participant_id TEXT,tool TEXT NOT NULL,arguments TEXT NOT NULL,bytes INTEGER NOT NULL,failed INTEGER NOT NULL)");
                    s.execute("CREATE INDEX mcp_calls_session ON mcp_calls(session_id,id)");
                    try (var rows = db.createStatement(); var r = rows.executeQuery(UTTERANCE_SELECT + " ORDER BY u.id")) {
                        while (r.next()) event(r.getString("session_id"), r.getLong("created_ms"), "utterance", utteranceRow(r));
                    }
                    s.execute("PRAGMA user_version=4");
                    db.commit();
                } catch (Exception e) { db.rollback(); throw e; }
                finally { db.setAutoCommit(true); }
            }
            if (version < 5) {
                db.setAutoCommit(false);
                try {
                    s.execute("CREATE TABLE privacy_rooms(session_id TEXT PRIMARY KEY,purpose_id TEXT NOT NULL,roster_version INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL CHECK(state IN ('pending','active','revoked','destroying','destroyed','legacy_blocked')),created_ms INTEGER NOT NULL,last_interaction_ms INTEGER NOT NULL,purpose_completed_ms INTEGER,retention_deadline_ms INTEGER NOT NULL,policy_version TEXT NOT NULL,hosted_disclosed INTEGER NOT NULL DEFAULT 0 CHECK(hosted_disclosed IN(0,1)))");
                    s.execute("CREATE TABLE biometric_consents(consent_id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES privacy_rooms(session_id),participant_id TEXT NOT NULL,subject_name TEXT NOT NULL,bipa_consent_granted INTEGER NOT NULL DEFAULT 0 CHECK(bipa_consent_granted IN(0,1)),consent_timestamp INTEGER,consent_method_version TEXT,notice_sha256 TEXT,signature_text TEXT,identity_method TEXT,subject_contact TEXT,operator_identity TEXT,opening_audio_sha256 TEXT,disclosure_scopes TEXT NOT NULL DEFAULT '[]',revoked_at_ms INTEGER,notice_text TEXT,last_interaction_ms INTEGER,retention_deadline_ms INTEGER,CHECK(bipa_consent_granted=0 OR (consent_timestamp IS NOT NULL AND consent_method_version IS NOT NULL AND notice_sha256 IS NOT NULL AND signature_text IS NOT NULL AND identity_method IS NOT NULL)),UNIQUE(session_id,participant_id))");
                    s.execute("CREATE TABLE consent_challenges(nonce_sha256 TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES privacy_rooms(session_id),participant_id TEXT NOT NULL,notice_sha256 TEXT NOT NULL,expires_at_ms INTEGER NOT NULL,consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN(0,1)))");
                    s.execute("CREATE TABLE consent_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,participant_id TEXT,timestamp_ms INTEGER NOT NULL,action TEXT NOT NULL,detail TEXT NOT NULL)");
                    s.execute("CREATE TABLE destruction_jobs(job_id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL UNIQUE REFERENCES privacy_rooms(session_id),state TEXT NOT NULL,created_ms INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,lease_until_ms INTEGER,last_error TEXT)");
                    s.execute("CREATE TABLE destruction_items(job_id INTEGER NOT NULL REFERENCES destruction_jobs(job_id),destination TEXT NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,receipt TEXT,last_error TEXT,PRIMARY KEY(job_id,destination))");
                    // Unknown old data is preserved but never silently authorized or auto-purged.
                    s.execute("INSERT INTO privacy_rooms(session_id,purpose_id,state,created_ms,last_interaction_ms,retention_deadline_ms,policy_version) SELECT id,'legacy_unknown','legacy_blocked',created_ms,created_ms,created_ms,'legacy_unknown' FROM sessions");
                    s.execute("PRAGMA user_version=5");
                    db.commit();
                } catch (Exception e) { db.rollback(); throw e; }
                finally { db.setAutoCommit(true); }
            }
        }
    }
    <T> T transaction(Work<T> work) {
        try {
            db.setAutoCommit(false);
            try { T result = work.run(); db.commit(); return result; }
            catch (Exception e) { db.rollback(); if (e instanceof RuntimeException r) throw r; throw new IllegalStateException(e); }
            finally { db.setAutoCommit(true); }
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    void execute(String sql, Object... args) throws SQLException {
        try (var p = prepare(sql, args)) { p.executeUpdate(); }
    }
    PreparedStatement prepare(String sql, Object... args) throws SQLException {
        var p = db.prepareStatement(sql);
        for (int i = 0; i < args.length; i++) p.setObject(i + 1, args[i]);
        return p;
    }
    Session session(String id) {
        try (var p = prepare("SELECT * FROM sessions WHERE id=?", id); var r = p.executeQuery()) {
            if (!r.next()) throw new ApiException(404, "session_not_found", "Session does not exist.");
            return new Session(id, r.getString("status"), r.getLong("next_sequence"), r.getLong("elapsed_ms"));
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    List<Profile> profiles(String session) {
        try (var p = prepare("SELECT * FROM profiles WHERE session_id=? ORDER BY id", session); var r = p.executeQuery()) {
            var list = new ArrayList<Profile>();
            while (r.next()) list.add(new Profile(r.getString("id"), r.getString("name"), r.getString("model"), vector(r.getString("anchor")), vector(r.getString("vector"))));
            return list;
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    ArrayNode participants(String session) {
        try (var p = prepare("SELECT id,name,kind,provider,model FROM participants WHERE session_id=? ORDER BY id", session); var r = p.executeQuery()) {
            var result = Json.arr();
            while (r.next()) result.add(Json.obj().put("id", r.getString(1)).put("name", r.getString(2)).put("kind", r.getString(3)).put("provider", r.getString(4)).put("model", r.getString(5)));
            return result;
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    ObjectNode participant(String session, String id) {
        for (var p : participants(session)) if (p.path("id").asText().equals(id)) return (ObjectNode) p;
        throw new ApiException(404, "speaker_not_found", "Participant is not registered in this session.");
    }
    static double[] vector(String json) {
        JsonNode node = Json.parse(json); double[] v = new double[node.size()];
        for (int i = 0; i < v.length; i++) v[i] = node.get(i).asDouble(); return v;
    }
    static String vectorJson(double[] v) { return Json.MAPPER.valueToTree(v).toString(); }
    ObjectNode segment(String session, long sequence) {
        try (var p = prepare("SELECT body FROM segments WHERE session_id=? AND sequence=?", session, sequence); var r = p.executeQuery()) {
            return r.next() ? (ObjectNode) Json.parse(r.getString(1)) : null;
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    ArrayNode transcript(String session, String speaker, long after, int limit) {
        try (var p = prepare("SELECT body FROM segments WHERE session_id=? AND sequence>? AND (? IS NULL OR speaker_id=?) ORDER BY sequence LIMIT ?", session, after, speaker, speaker, limit); var r = p.executeQuery()) {
            var result = Json.arr(); while (r.next()) result.add(Json.parse(r.getString(1))); return result;
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    ArrayNode sessions(int limit) {
        try (var p = prepare("SELECT s.id,s.status,s.created_ms,s.elapsed_ms,(SELECT group_concat(id||'='||name,', ') FROM (SELECT id,name FROM participants WHERE session_id=s.id ORDER BY id)) AS who FROM sessions s ORDER BY s.created_ms DESC LIMIT ?", limit); var r = p.executeQuery()) {
            var result = Json.arr();
            while (r.next()) result.add(Json.obj().put("session_id", r.getString(1)).put("status", r.getString(2)).put("created_ms", r.getLong(3)).put("elapsed_ms", r.getLong(4)).put("participants", r.getString(5)));
            return result;
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    ArrayNode utterances(String session, long after, int limit, int minRank) {
        try (var p = prepare(UTTERANCE_SELECT + " WHERE u.session_id=? AND u.id>? AND (CASE u.label WHEN 'agent' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END)>=? ORDER BY u.id LIMIT ?", session, after, minRank, limit); var r = p.executeQuery()) {
            var result = Json.arr();
            while (r.next()) result.add(utteranceRow(r));
            return result;
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    private static final String UTTERANCE_SELECT = "SELECT u.id,u.speaker_id,p.name,u.start_ms,u.end_ms,u.text,u.source,u.similarity,u.margin,u.overlap_ratio,u.abstain_ratio,u.label,u.candidates,u.session_id,u.created_ms FROM utterances u LEFT JOIN participants p ON p.session_id=u.session_id AND p.id=u.speaker_id";
    private static ObjectNode utteranceRow(ResultSet r) throws SQLException {
        var row = Json.obj().put("utterance_id", r.getLong(1)).put("speaker_id", r.getString(2)).put("speaker_name", r.getString(3))
            .put("start_ms", r.getLong(4)).put("end_ms", r.getLong(5)).put("text", r.getString(6)).put("source", r.getString(7));
        for (int i = 8; i <= 11; i++) { double v = r.getDouble(i); if (r.wasNull()) row.putNull(COLUMNS[i - 8]); else row.put(COLUMNS[i - 8], v); }
        row.put("label", r.getString(12));
        String candidates = r.getString(13);
        row.set("candidates", candidates == null ? Json.arr() : Json.parse(candidates));
        return row;
    }
    ObjectNode utterance(String session, long id) throws SQLException {
        try (var p = prepare(UTTERANCE_SELECT + " WHERE u.session_id=? AND u.id=?", session, id); var r = p.executeQuery()) {
            if (!r.next()) throw new IllegalStateException("Stored utterance missing");
            return utteranceRow(r);
        }
    }
    void event(String session, long now, String type, JsonNode data) throws SQLException {
        execute("INSERT INTO events(session_id,timestamp_ms,type,data) VALUES(?,?,?,?)", session, now, type, data.toString());
    }
    ArrayNode events(String session, long after, int limit) {
        try (var p = prepare("SELECT id,timestamp_ms,type,data FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT ?", session, after, limit); var r = p.executeQuery()) {
            var result = Json.arr();
            while (r.next()) result.add(Json.obj().put("event_id", r.getLong(1)).put("timestamp_ms", r.getLong(2)).put("type", r.getString(3)).set("data", Json.parse(r.getString(4))));
            return result;
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    long lastInsertId() throws SQLException {
        try (var p = prepare("SELECT last_insert_rowid()"); var r = p.executeQuery()) { r.next(); return r.getLong(1); }
    }
    Lease lease(String session) throws SQLException {
        try (var p = prepare("SELECT participant_id,expires_at_ms FROM floor WHERE session_id=?", session); var r = p.executeQuery()) {
            return r.next() ? new Lease(r.getString(1), r.getLong(2)) : null;
        }
    }
    private static final String[] COLUMNS = {"similarity", "margin", "overlap_ratio", "abstain_ratio"};
    /** Candidate names for an overlap row, e.g. "Alice+Bob". */
    String names(String session, JsonNode candidates) {
        var byId = new LinkedHashMap<String, String>();
        for (var p : participants(session)) byId.put(p.path("id").asText(), p.path("name").asText());
        var parts = new ArrayList<String>();
        for (JsonNode c : candidates) parts.add(byId.getOrDefault(c.asText(), c.asText()));
        return String.join("+", parts);
    }
    ArrayNode corrections(String session, long after, int limit) {
        try (var p = prepare("SELECT * FROM corrections WHERE session_id=? AND id>? ORDER BY id LIMIT ?", session, after, limit); var r = p.executeQuery()) {
            var result = Json.arr();
            while (r.next()) result.add(Json.obj().put("correction_id", r.getLong("id")).put("segment_id", r.getString("segment_id"))
                .put("previous_speaker", r.getString("previous_speaker")).put("actual_speaker", r.getString("actual_speaker"))
                .put("timestamp_ms", r.getLong("created_ms")).put("profile_updated", r.getBoolean("profile_updated")));
            return result;
        } catch (SQLException e) { throw new IllegalStateException(e); }
    }
    void rebuildProfiles(String session) throws SQLException {
        for (Profile p : profiles(session)) {
            double[] sum = new double[p.anchor.length]; int count = 0;
            try (var q = prepare("SELECT embedding FROM correction_examples WHERE session_id=? AND speaker_id=?", session, p.id); var r = q.executeQuery()) {
                while (r.next()) { double[] v = vector(r.getString(1)); for (int i = 0; i < sum.length; i++) sum[i] += v[i]; count++; }
            }
            // Corrections jointly contribute at most 20%; repeated edits never compound drift.
            double[] updated = p.anchor.clone();
            if (count > 0) for (int i = 0; i < updated.length; i++) updated[i] = .8 * updated[i] + .2 * sum[i] / count;
            execute("UPDATE profiles SET vector=? WHERE session_id=? AND id=?", vectorJson(Audio.normalize(updated)), session, p.id);
        }
    }
    public void close() throws SQLException { db.close(); }
}
