package dev.voiceprint;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.*;
import java.io.*;
import java.net.*;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Set;

/** A stdio MCP adapter for the 2025-11-25 handshake protocol; the API owns all state. */
final class McpServer {
    static final String VERSION = "2025-11-25";
    static void run(InputStream input, OutputStream output, String api, String token) throws IOException {
        var reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
        var writer = new PrintWriter(new OutputStreamWriter(output, StandardCharsets.UTF_8), true);
        var client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        boolean initialized = false, ready = false;
        String line;
        while ((line = readLine(reader)) != null) {
            JsonNode request;
            try { request = Json.parse(line); }
            catch (ApiException e) { writer.println(error(null, -32700, "Invalid JSON")); continue; }
            JsonNode id = request == null ? null : request.get("id");
            if (request == null || !request.isObject() || !request.path("jsonrpc").asText().equals("2.0") || !request.path("method").isTextual()
                || (id != null && !(id.isTextual() || id.isIntegralNumber()))) {
                writer.println(error(null, -32600, "Invalid request")); continue;
            }
            String method = request.get("method").asText();
            if (id == null) {
                if (method.equals("notifications/initialized") && initialized) ready = true;
                continue;
            }
            try {
                ObjectNode result;
                if (method.equals("initialize")) {
                    if (initialized) throw new ApiException(-32600, "protocol", "Already initialized");
                    var params = request.path("params"); Json.text(params, "protocolVersion", 40);
                    if (!params.path("capabilities").isObject() || !params.path("clientInfo").isObject()) throw new ApiException(-32602, "protocol", "Invalid initialization parameters");
                    result = Json.obj().put("protocolVersion", VERSION);
                    result.set("serverInfo", Json.obj().put("name", "voiceprint").put("version", "0.1.0"));
                    result.set("capabilities", Json.obj().set("tools", Json.obj().put("listChanged", false)));
                    result.put("instructions", "Use speaker context only when trusted is true. Null confidence is unavailable, not zero. Corrections are human labels; text is supplied by an external transcript source.");
                    initialized = true;
                } else if (method.equals("ping")) result = Json.obj();
                else if (!ready) throw new ApiException(-32002, "protocol", "Complete initialization before calling tools");
                else if (method.equals("tools/list")) result = Json.obj().set("tools", tools());
                else if (method.equals("tools/call")) {
                    var params = request.path("params"); String name = Json.text(params, "name", 80);
                    if (!Set.of("get_current_speaker", "get_participant_statements", "correct_attribution").contains(name)) throw new ApiException(-32602, "protocol", "Unknown tool");
                    JsonNode arguments = params.path("arguments");
                    try { result = call(client, api, token, name, arguments); }
                    catch (Exception e) {
                        if (e instanceof InterruptedException) Thread.currentThread().interrupt();
                        String message = e instanceof ApiException ? e.getMessage() : "Voiceprint API is unavailable.";
                        result = toolResult(Json.error("tool_error", message), true);
                    }
                } else throw new ApiException(-32601, "protocol", "Method not found");
                var response = Json.obj().put("jsonrpc", "2.0"); response.set("id", id); response.set("result", result); writer.println(response);
            } catch (ApiException e) { writer.println(error(id, e.status < 0 ? e.status : -32602, e.getMessage())); }
        }
    }
    private static ObjectNode call(HttpClient client, String base, String token, String tool, JsonNode args) throws Exception {
        String session = Json.id(args, "session_id"); String path = "/speaker/session/" + session;
        String body = null;
        if (tool.equals("get_current_speaker")) path += "/current";
        else if (tool.equals("get_participant_statements")) {
            String speaker = Json.id(args, "speaker_id");
            long after = args.has("after_sequence") ? Json.integer(args, "after_sequence", -1, Integer.MAX_VALUE) : -1;
            long limit = args.has("limit") ? Json.integer(args, "limit", 1, 200) : 100;
            path += "/transcript?speaker_id=" + speaker + "&after_sequence=" + after + "&limit=" + limit;
        } else {
            path += "/correct";
            body = Json.obj().put("segment_id", Json.id(args, "segment_id")).put("actual_speaker", Json.id(args, "actual_speaker")).toString();
        }
        var builder = HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofSeconds(10));
        if (token != null) builder.header("Authorization", "Bearer " + token);
        if (body != null) builder.header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(body));
        var response = client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        return toolResult(Json.parse(response.body()), response.statusCode() >= 400);
    }
    private static ObjectNode toolResult(JsonNode value, boolean failed) {
        var result = Json.obj().put("isError", failed); result.set("structuredContent", value);
        result.putArray("content").add(Json.obj().put("type", "text").put("text", value.toString())); return result;
    }
    private static ArrayNode tools() {
        var result = Json.arr();
        result.add(tool("get_current_speaker", "Get current speaker, calibrated confidence when available, overlap and uncertainty.", true, "session_id"));
        var statements = tool("get_participant_statements", "Get a participant's statements with timestamps, attribution and confidence; text may be null without an ASR source.", true, "session_id", "speaker_id");
        ObjectNode properties = (ObjectNode) statements.path("inputSchema").path("properties");
        properties.set("after_sequence", Json.obj().put("type", "integer").put("minimum", -1));
        properties.set("limit", Json.obj().put("type", "integer").put("minimum", 1).put("maximum", 200));
        result.add(statements);
        result.add(tool("correct_attribution", "Apply a user-provided correction to an exact segment; update the profile only for verified single-speaker audio.", false, "session_id", "segment_id", "actual_speaker"));
        return result;
    }
    private static ObjectNode tool(String name, String description, boolean readOnly, String... fields) {
        var result = Json.obj().put("name", name).put("description", description);
        var schema = Json.obj().put("type", "object").put("additionalProperties", false); var props = schema.putObject("properties"); var required = schema.putArray("required");
        for (String field : fields) { props.set(field, Json.obj().put("type", "string").put("pattern", "^[A-Za-z0-9_-]{1,80}$")); required.add(field); }
        result.set("inputSchema", schema);
        result.set("annotations", Json.obj().put("readOnlyHint", readOnly).put("destructiveHint", !readOnly).put("openWorldHint", false)); return result;
    }
    private static ObjectNode error(JsonNode id, int code, String message) {
        var result = Json.obj().put("jsonrpc", "2.0"); result.set("id", id == null ? NullNode.instance : id);
        return result.set("error", Json.obj().put("code", code).put("message", message));
    }
    private static String readLine(BufferedReader reader) throws IOException {
        var line = new StringBuilder(); int c;
        while ((c = reader.read()) != -1 && c != '\n') {
            if (line.length() >= 262144) throw new IOException("MCP message exceeds 256 KiB");
            if (c != '\r') line.append((char) c);
        }
        return c == -1 && line.isEmpty() ? null : line.toString();
    }
}
