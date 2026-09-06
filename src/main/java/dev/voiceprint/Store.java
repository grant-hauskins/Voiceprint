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
    interface Work<T> { T run() throws Exception; }
    Store(Path path) throws Exception {
        Path parent = path.toAbsolutePath().getParent(); Files.createDirectories(parent);
        db = DriverManager.getConnection("jdbc:sqlite:" + path.toAbsolutePath());
        try (var s = db.createStatement()) {
            s.execute("PRAGMA foreign_keys=ON"); s.execute("PRAGMA journal_mode=WAL"); s.execute("PRAGMA busy_timeout=3000");
            int version; try (var r = s.executeQuery("PRAGMA user_version")) { version = r.getInt(1); }
            if (version > 1) throw new IllegalStateException("Database schema is newer than this application");
            s.execute("CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,status TEXT NOT NULL,created_ms INTEGER NOT NULL,next_sequence INTEGER NOT NULL DEFAULT 0,elapsed_ms INTEGER NOT NULL DEFAULT 0)");
            s.execute("CREATE TABLE IF NOT EXISTS profiles(session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,id TEXT NOT NULL,name TEXT NOT NULL,model TEXT NOT NULL,anchor TEXT NOT NULL,vector TEXT NOT NULL,PRIMARY KEY(session_id,id))");
            s.execute("CREATE TABLE IF NOT EXISTS segments(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,sequence INTEGER NOT NULL,hash TEXT NOT NULL,speaker_id TEXT,body TEXT NOT NULL,embedding TEXT,eligible INTEGER NOT NULL,UNIQUE(session_id,sequence))");
            s.execute("CREATE INDEX IF NOT EXISTS segments_speaker ON segments(session_id,speaker_id,sequence)");
            s.execute("CREATE TABLE IF NOT EXISTS corrections(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,segment_id TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,previous_speaker TEXT,actual_speaker TEXT NOT NULL,created_ms INTEGER NOT NULL,profile_updated INTEGER NOT NULL)");
            s.execute("CREATE INDEX IF NOT EXISTS corrections_session ON corrections(session_id,id)");
            s.execute("CREATE TABLE IF NOT EXISTS correction_examples(segment_id TEXT PRIMARY KEY REFERENCES segments(id) ON DELETE CASCADE,session_id TEXT NOT NULL,speaker_id TEXT NOT NULL,embedding TEXT NOT NULL,FOREIGN KEY(session_id,speaker_id) REFERENCES profiles(session_id,id) ON DELETE CASCADE)");
            s.execute("PRAGMA user_version=1");
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
