package dev.voiceprint;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.*;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;

/**
 * MCP Streamable HTTP transport (spec 2025-11-25), stateless and tools-only, for hosted agents such as the
 * OpenAI Realtime/Responses APIs reached through a public tunnel. Every request is answered with a single
 * application/json body; SSE is never used because tunnels commonly buffer it. GET returns 405.
 */
final class McpHttpServer implements AutoCloseable {
    private final HttpServer server;
    private final ThreadPoolExecutor executor;
    private final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    private final String api, apiToken, mcpToken;
    McpHttpServer(int port, String api, String apiToken, String mcpToken) throws IOException {
        this.api = api; this.apiToken = apiToken; this.mcpToken = mcpToken;
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 16);
        executor = new ThreadPoolExecutor(4, 4, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(32), new ThreadPoolExecutor.AbortPolicy());
        server.setExecutor(executor); server.createContext("/mcp", this::handle);
    }
    void start() { server.start(); }
    int port() { return server.getAddress().getPort(); }
    private void handle(HttpExchange exchange) throws IOException {
        try {
            var headers = exchange.getRequestHeaders();
            // Browser-origin requests are never legitimate here; server-to-server clients send no Origin.
            if (headers.containsKey("Origin")) { respond(exchange, 403, Json.error("origin_rejected", "Browser origins are not accepted.")); return; }
            if (mcpToken != null && !MessageDigest.isEqual(("Bearer " + mcpToken).getBytes(StandardCharsets.UTF_8),
                Optional.ofNullable(headers.getFirst("Authorization")).orElse("").getBytes(StandardCharsets.UTF_8))) {
                respond(exchange, 401, Json.error("unauthorized", "A valid bearer token is required.")); return;
            }
            String method = exchange.getRequestMethod();
            if (!method.equals("POST")) { exchange.getResponseHeaders().set("Allow", "POST"); respond(exchange, 405, Json.error("method_not_allowed", "Use POST for MCP messages.")); return; }
            String version = headers.getFirst("MCP-Protocol-Version");
            if (version != null && !McpServer.VERSIONS.contains(version)) { respond(exchange, 400, Json.error("unsupported_protocol_version", "Supported: " + McpServer.VERSIONS)); return; }
            String contentType = headers.getFirst("Content-Type");
            if (contentType == null || !contentType.toLowerCase(Locale.ROOT).startsWith("application/json")) { respond(exchange, 415, Json.error("unsupported_media_type", "Use application/json.")); return; }
            byte[] bytes = exchange.getRequestBody().readNBytes(262145);
            if (bytes.length > 262144) { respond(exchange, 413, Json.error("body_too_large", "MCP message exceeds 256 KiB.")); return; }
            JsonNode message;
            try { message = Json.parse(new String(bytes, StandardCharsets.UTF_8)); }
            catch (ApiException e) { respond(exchange, 400, McpServer.error(null, -32700, "Invalid JSON")); return; }
            if (message == null || !message.isObject()) { respond(exchange, 400, McpServer.error(null, -32600, "Expected a single JSON-RPC object; batches are not supported.")); return; }
            ObjectNode response = McpServer.dispatch(message, client, api, apiToken, null);
            if (response == null) { exchange.sendResponseHeaders(202, -1); return; }
            // Independent evidence that a hosted agent used the tool: written by this process, not reported by the agent.
            String rpc = message.path("method").asText();
            String via = Optional.ofNullable(headers.getFirst("Cf-Connecting-Ip")).orElse(exchange.getRemoteAddress().getAddress().getHostAddress());
            String detail = rpc.equals("tools/call") ? message.path("params").path("name").asText() + " " + message.path("params").path("arguments").toString() : "";
            int size = response.toString().getBytes(StandardCharsets.UTF_8).length;
            boolean failed = response.has("error") || response.path("result").path("isError").asBoolean(false);
            System.err.println(java.time.LocalTime.now().withNano(0) + " MCP " + rpc + " " + detail + " from " + via + " -> " + (failed ? "error" : "ok") + " " + size + " bytes");
            respond(exchange, 200, response);
        } catch (Exception e) { System.err.println("MCP request failed: " + e.getClass().getSimpleName()); respond(exchange, 500, Json.error("internal_error", "Request could not be completed.")); }
        finally { exchange.close(); }
    }
    private static void respond(HttpExchange exchange, int status, ObjectNode body) throws IOException {
        byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        exchange.sendResponseHeaders(status, bytes.length); exchange.getResponseBody().write(bytes);
    }
    public void close() { server.stop(0); executor.shutdownNow(); }
}
