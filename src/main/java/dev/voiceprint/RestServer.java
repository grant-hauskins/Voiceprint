package dev.voiceprint;

import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;

final class RestServer implements AutoCloseable {
    private final HttpServer server;
    private final ThreadPoolExecutor executor;
    private final SpeakerService service;
    private final String token;
    RestServer(SpeakerService service, int port, String token) throws IOException {
        this.service = service; this.token = token;
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 16);
        executor = new ThreadPoolExecutor(4, 4, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(16), new ThreadPoolExecutor.AbortPolicy());
        server.setExecutor(executor); server.createContext("/", this::handle);
    }
    void start() { server.start(); }
    int port() { return server.getAddress().getPort(); }
    private void handle(HttpExchange exchange) throws IOException {
        try {
            String path = exchange.getRequestURI().getPath();
            // Disallow browser-origin access and unexpected Host headers (including DNS rebinding).
            String host = exchange.getRequestHeaders().getFirst("Host");
            if (host == null || !host.matches("(127\\.0\\.0\\.1|localhost)(:[0-9]+)?") || exchange.getRequestHeaders().containsKey("Origin"))
                throw new ApiException(403, "origin_rejected", "This API only accepts local, non-browser clients.");
            if (token != null && !MessageDigest.isEqual(("Bearer " + token).getBytes(StandardCharsets.UTF_8),
                Optional.ofNullable(exchange.getRequestHeaders().getFirst("Authorization")).orElse("").getBytes(StandardCharsets.UTF_8)))
                throw new ApiException(401, "unauthorized", "A valid bearer token is required.");
            String method = exchange.getRequestMethod();
            if (path.equals("/health") && method.equals("GET")) { respond(exchange, 200, Json.obj().put("status", "ok").put("service", "voiceprint").put("model_readiness", "checked_on_inference")); return; }
            ObjectNode body = Json.obj();
            if (method.equals("POST")) {
                String contentType = exchange.getRequestHeaders().getFirst("Content-Type");
                if (contentType == null || !contentType.toLowerCase(Locale.ROOT).startsWith("application/json")) throw new ApiException(415, "unsupported_media_type", "Use application/json.");
                byte[] bytes = exchange.getRequestBody().readNBytes(2_700_001);
                if (bytes.length > 2_700_000) throw new ApiException(413, "body_too_large", "Request exceeds the enrollment size limit.");
                var parsed = Json.parse(new String(bytes, StandardCharsets.UTF_8));
                if (!(parsed instanceof ObjectNode object)) throw new ApiException(400, "invalid_input", "Expected a JSON object.");
                body = object;
            }
            if (path.equals("/speaker/session/init") && method.equals("POST")) { respond(exchange, 201, service.init(body)); return; }
            String[] parts = path.split("/");
            if (parts.length < 4 || !parts[1].equals("speaker") || !parts[2].equals("session")) throw new ApiException(404, "not_found", "Endpoint does not exist.");
            String session = parts[3];
            if (!session.matches("[A-Za-z0-9_-]{1,80}")) throw new ApiException(400, "invalid_session", "Invalid session ID.");
            Map<String, String> query = query(exchange.getRequestURI().getRawQuery());
            String action = parts.length == 5 ? parts[4] : "";
            ObjectNode result;
            if (parts.length == 4 && method.equals("DELETE")) result = service.delete(session);
            else if (parts.length != 5) throw new ApiException(404, "not_found", "Endpoint does not exist.");
            else result = switch (method + " " + action) {
                case "POST audio" -> service.ingest(session, body);
                case "GET current" -> service.current(session);
                case "GET profiles" -> service.profiles(session);
                case "GET transcript" -> service.transcript(session, query.get("speaker_id"), number(query, "after_sequence", -1), (int) number(query, "limit", 100));
                case "GET corrections" -> service.corrections(session, number(query, "after_id", 0), (int) number(query, "limit", 100));
                case "POST correct" -> service.correct(session, body);
                case "POST end" -> service.end(session);
                default -> throw new ApiException(404, "not_found", "Endpoint does not exist.");
            };
            respond(exchange, 200, result);
        } catch (ApiException e) { respond(exchange, e.status, Json.error(e.code, e.getMessage())); }
        catch (Exception e) { System.err.println("Request failed: " + e.getClass().getSimpleName()); respond(exchange, 500, Json.error("internal_error", "Request could not be completed.")); }
        finally { exchange.close(); }
    }
    private static Map<String, String> query(String raw) {
        Map<String, String> result = new HashMap<>();
        if (raw != null && !raw.isEmpty()) for (String pair : raw.split("&")) {
            String[] parts = pair.split("=", 2);
            try { result.put(URLDecoder.decode(parts[0], StandardCharsets.UTF_8), URLDecoder.decode(parts.length > 1 ? parts[1] : "", StandardCharsets.UTF_8)); }
            catch (IllegalArgumentException e) { throw new ApiException(400, "invalid_query", "Invalid URL encoding."); }
        }
        return result;
    }
    private static long number(Map<String, String> query, String key, long fallback) {
        try { long n = Long.parseLong(query.getOrDefault(key, Long.toString(fallback))); if (key.equals("limit") && (n < 1 || n > 200)) throw new NumberFormatException(); return n; }
        catch (NumberFormatException e) { throw new ApiException(400, "invalid_query", key + " is outside the allowed integer range."); }
    }
    private static void respond(HttpExchange exchange, int status, ObjectNode body) throws IOException {
        byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        exchange.sendResponseHeaders(status, bytes.length); exchange.getResponseBody().write(bytes);
    }
    public void close() { server.stop(0); executor.shutdownNow(); }
}
