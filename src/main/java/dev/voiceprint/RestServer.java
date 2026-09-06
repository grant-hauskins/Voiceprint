package dev.voiceprint;

import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;

final class RestServer implements AutoCloseable {
    private final HttpServer server;
    private final ExecutorService executor;
    private final SpeakerService service;
    private final String token;
    private final Path webRoot;
    RestServer(SpeakerService service, int port, String token) throws IOException {
        this(service, port, token, Path.of("web"));
    }
    RestServer(SpeakerService service, int port, String token, Path webRoot) throws IOException {
        this.service = service; this.token = token;
        this.webRoot = webRoot.toAbsolutePath().normalize();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 16);
        // Long-poll clients must never occupy every worker available to audio ingestion.
        executor = Executors.newVirtualThreadPerTaskExecutor();
        server.setExecutor(executor); server.createContext("/", this::handle);
    }
    void start() { server.start(); }
    int port() { return server.getAddress().getPort(); }
    private void handle(HttpExchange exchange) throws IOException {
        try {
            String path = exchange.getRequestURI().getPath();
            // Browser requests are same-origin only; Host still prevents DNS rebinding.
            String host = exchange.getRequestHeaders().getFirst("Host");
            var origins = exchange.getRequestHeaders().get("Origin");
            String suffix = ":" + port();
            boolean validHost = ("127.0.0.1" + suffix).equals(host) || ("localhost" + suffix).equals(host)
                || (port() == 80 && ("127.0.0.1".equals(host) || "localhost".equals(host)));
            if (!validHost || exchange.getRequestHeaders().get("Host").size() != 1
                || (origins != null && (origins.size() != 1 || !origins.getFirst().equals("http://" + host))))
                throw new ApiException(403, "origin_rejected", "This API only accepts its exact local origin and listening port.");
            String method = exchange.getRequestMethod();
            if (path.equals("/ui") || path.startsWith("/ui/")) { serveStatic(exchange, path, method); return; }
            if (path.equals("/privacy/notice") && method.equals("GET")) { respond(exchange, 200, service.privacyNotice()); return; }
            if (token != null && !MessageDigest.isEqual(("Bearer " + token).getBytes(StandardCharsets.UTF_8),
                Optional.ofNullable(exchange.getRequestHeaders().getFirst("Authorization")).orElse("").getBytes(StandardCharsets.UTF_8)))
                throw new ApiException(401, "unauthorized", "A valid bearer token is required.");
            if (path.equals("/health") && method.equals("GET")) { respond(exchange, 200, Json.obj().put("status", "ok").put("service", "voiceprint").put("model_readiness", "checked_on_inference")); return; }
            if (token == null || token.isBlank()) throw new ApiException(503, "operator_auth_required", "Configure an operator API token before using privacy or conversation endpoints.");
            boolean consentWrite = method.equals("POST") && (path.equals("/privacy/rooms") || path.contains("/consents/"));
            if (consentWrite && origins == null) throw new ApiException(403, "origin_rejected", "Consent submission requires the exact local browser origin.");
            String admissionSession = null;
            String scope = "true".equals(exchange.getRequestHeaders().getFirst("X-Voiceprint-Hosted-MCP")) ? "hosted_mcp" : "local_processing";
            if (path.equals("/speaker/session/init") && method.equals("POST")) {
                admissionSession = exchange.getRequestHeaders().getFirst("X-Voiceprint-Session");
                if (admissionSession == null || !admissionSession.matches("[A-Za-z0-9_-]{1,80}")) throw new ApiException(403, "prior_written_release_required", "Supply the authorized room in X-Voiceprint-Session before sending enrollment audio.");
            } else {
                String[] admission = path.split("/");
                if (admission.length == 5 && admission[1].equals("speaker") && admission[2].equals("session")
                    && Set.of("audio", "utterances", "correct", "current", "profiles", "participants", "transcript", "corrections", "floor", "events").contains(admission[4])) admissionSession = admission[3];
            }
            // Admission happens before the app reads, decodes or hashes any unauthorized audio body.
            if (admissionSession != null) service.requireConsent(admissionSession, scope);
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
            if (path.equals("/privacy/rooms") && method.equals("POST")) { respond(exchange, 201, service.createPrivacyRoom(body)); return; }
            if (path.equals("/privacy/rooms") && method.equals("GET")) { respond(exchange, 200, service.privacyRooms()); return; }
            if (path.equals("/speaker/session/init") && method.equals("POST")) {
                if (!body.path("session_id").asText().equals(admissionSession)) throw new ApiException(403, "roster_mismatch", "Enrollment room does not match its authorization header.");
                synchronized (service) { respond(exchange, 201, service.init(body)); } return;
            }
            if (path.equals("/speaker/sessions") && method.equals("GET")) { synchronized (service) { respond(exchange, 200, service.sessions((int) number(query(exchange.getRequestURI().getRawQuery()), "limit", 20), scope.equals("hosted_mcp"))); } return; }
            String[] parts = path.split("/");
            if (parts.length < 4 || !parts[1].equals("speaker") || !parts[2].equals("session")) throw new ApiException(404, "not_found", "Endpoint does not exist.");
            String session = parts[3];
            if (!session.matches("[A-Za-z0-9_-]{1,80}")) throw new ApiException(400, "invalid_session", "Invalid session ID.");
            Map<String, String> query = query(exchange.getRequestURI().getRawQuery());
            String action = parts.length == 5 ? parts[4] : "";
            if (parts.length >= 6 && parts[4].equals("consents") && method.equals("POST")) {
                String participant = Json.id(Json.obj().put("participant_id", parts[5]), "participant_id");
                if (parts.length == 6) { respond(exchange, 201, service.consentRelease(session, participant, body)); return; }
                if (parts.length == 7 && parts[6].equals("challenge")) { respond(exchange, 200, service.consentChallenge(session, participant)); return; }
                if (parts.length == 7 && parts[6].equals("revoke")) { respond(exchange, 200, service.consentRevoke(session, participant)); return; }
                throw new ApiException(404, "not_found", "Endpoint does not exist.");
            }
            if (parts.length == 5 && action.equals("participants") && method.equals("POST")) {
                var registration = service.register(session, body);
                respond(exchange, registration.created() ? 201 : 200, registration.response()); return;
            }
            ObjectNode result;
            if (parts.length == 4 && method.equals("DELETE")) result = service.delete(session);
            else if (parts.length != 5) throw new ApiException(404, "not_found", "Endpoint does not exist.");
            else result = switch (method + " " + action) {
                case "POST audio" -> service.ingest(session, body);
                case "GET current" -> service.current(session);
                case "GET profiles" -> service.profiles(session);
                case "GET participants" -> service.participants(session);
                case "GET consent" -> service.consentStatus(session);
                case "GET destruction" -> service.destructionStatus(session);
                case "POST floor" -> service.claimFloor(session, body);
                case "GET floor" -> service.floor(session);
                case "DELETE floor" -> service.releaseFloor(session, query.get("participant_id"));
                case "GET events" -> service.events(session, number(query, "after_id", 0), (int) number(query, "limit", 100), number(query, "wait_ms", 0));
                case "GET transcript" -> service.transcript(session, query.get("speaker_id"), number(query, "after_sequence", -1), (int) number(query, "limit", 100));
                case "GET corrections" -> service.corrections(session, number(query, "after_id", 0), (int) number(query, "limit", 100));
                case "POST correct" -> service.correct(session, body);
                case "POST utterances" -> service.utter(session, body);
                case "GET utterances" -> service.utterances(session, number(query, "after_id", 0), (int) number(query, "limit", 100), query.get("min_label"));
                case "POST end" -> service.end(session);
                default -> throw new ApiException(404, "not_found", "Endpoint does not exist.");
            };
            synchronized (service) {
                if (admissionSession != null) service.requireConsent(admissionSession, scope);
                respond(exchange, 200, result);
            }
        } catch (ApiException e) { respond(exchange, e.status, Json.error(e.code, e.getMessage())); }
        catch (Exception e) { System.err.println("Request failed: " + e.getClass().getSimpleName()); respond(exchange, 500, Json.error("internal_error", "Request could not be completed.")); }
        finally { exchange.close(); }
    }
    private void serveStatic(HttpExchange exchange, String path, String method) throws IOException {
        if (!method.equals("GET") && !method.equals("HEAD")) throw new ApiException(405, "method_not_allowed", "Use GET for UI assets.");
        String relative = path.equals("/ui") || path.equals("/ui/") ? "index.html" : path.substring(4);
        // Reject decoded traversal too, and second-encoding tricks instead of decoding a second time.
        if (relative.indexOf('\\') >= 0 || relative.indexOf(':') >= 0 || relative.indexOf('%') >= 0 || relative.indexOf('\0') >= 0)
            throw new ApiException(403, "invalid_asset_path", "Invalid UI asset path.");
        for (String part : relative.split("/", -1))
            if (part.isEmpty() || part.equals(".") || part.equals("..") || part.endsWith(".") || part.endsWith(" "))
                throw new ApiException(403, "invalid_asset_path", "Invalid UI asset path.");
        Path target;
        try { target = webRoot.resolve(relative).normalize(); }
        catch (InvalidPathException e) { throw new ApiException(403, "invalid_asset_path", "Invalid UI asset path."); }
        if (!target.startsWith(webRoot)) throw new ApiException(403, "invalid_asset_path", "Invalid UI asset path.");
        if (!Files.exists(target) || !Files.isDirectory(webRoot)) throw new ApiException(404, "not_found", "UI asset does not exist.");
        Path real = target.toRealPath();
        if (!real.startsWith(webRoot.toRealPath())) throw new ApiException(403, "invalid_asset_path", "UI symlinks must remain inside web.");
        if (!Files.isRegularFile(real)) throw new ApiException(404, "not_found", "UI asset is not a file.");
        String name = real.getFileName().toString().toLowerCase(Locale.ROOT);
        String extension = name.substring(name.lastIndexOf('.') + 1);
        String mime = switch (extension) {
            case "html" -> "text/html; charset=utf-8";
            case "js", "mjs" -> "text/javascript; charset=utf-8";
            case "css" -> "text/css; charset=utf-8";
            case "json" -> "application/json; charset=utf-8";
            case "svg" -> "image/svg+xml";
            case "png" -> "image/png";
            case "jpg", "jpeg" -> "image/jpeg";
            case "ico" -> "image/x-icon";
            case "woff" -> "font/woff";
            case "woff2" -> "font/woff2";
            default -> "application/octet-stream";
        };
        var headers = exchange.getResponseHeaders();
        headers.set("Content-Type", mime); headers.set("Cache-Control", "no-store"); headers.set("X-Content-Type-Options", "nosniff");
        if (method.equals("HEAD")) { headers.set("Content-Length", Long.toString(Files.size(real))); exchange.sendResponseHeaders(200, -1); }
        else { byte[] bytes = Files.readAllBytes(real); exchange.sendResponseHeaders(200, bytes.length); exchange.getResponseBody().write(bytes); }
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
