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
    static final Set<String> VERSIONS = Set.of("2025-03-26", "2025-06-18", "2025-11-25");
    static final Set<String> TOOLS = Set.of("list_sessions", "get_transcript", "get_current_speaker", "get_participant_statements", "correct_attribution", "get_agent_channel", "post_agent_channel");
    static final String LABEL_NOTICE = "Human labels high/medium/low are similarity-based, not calibrated probabilities. Agent labels are declared by their registered producer. Null confidence is unavailable, not zero.";

    /** Per-connection state for stdio, where the lifecycle handshake is enforced. HTTP is stateless and skips it. */
    static final class Session { boolean initialized, ready; }

    static void run(InputStream input, OutputStream output, String api, String token) throws IOException {
        var reader = new BufferedReader(new InputStreamReader(input, StandardCharsets.UTF_8));
        var writer = new PrintWriter(new OutputStreamWriter(output, StandardCharsets.UTF_8), true);
        var client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        var session = new Session();
        String line;
        while ((line = readLine(reader)) != null) {
            JsonNode request;
            try { request = Json.parse(line); }
            catch (ApiException e) { writer.println(error(null, -32700, "Invalid JSON")); continue; }
            ObjectNode response = dispatch(request, client, api, token, session);
            if (response != null) writer.println(response);
        }
    }

    /**
     * Handles one JSON-RPC message. Returns null for notifications (nothing to send). With a non-null `session`
     * the stdio lifecycle is enforced (initialize before tools); with null every request stands alone.
     */
    static ObjectNode dispatch(JsonNode request, HttpClient client, String api, String token, Session session) {
        return dispatch(request, client, api, token, session, false, null);
    }
    static ObjectNode dispatch(JsonNode request, HttpClient client, String api, String token, Session session, boolean hosted) {
        return dispatch(request, client, api, token, session, hosted, null);
    }
    /** `participant` is the MCP URL's caller-declared participant_id tag (null on stdio); only post_agent_channel uses it. */
    static ObjectNode dispatch(JsonNode request, HttpClient client, String api, String token, Session session, boolean hosted, String participant) {
        JsonNode id = request == null ? null : request.get("id");
        if (request == null || !request.isObject() || !request.path("jsonrpc").asText().equals("2.0") || !request.path("method").isTextual()
            || (id != null && !(id.isTextual() || id.isIntegralNumber())))
            return error(null, -32600, "Invalid request");
        String method = request.get("method").asText();
        if (id == null) {
            if (method.equals("notifications/initialized") && session != null && session.initialized) session.ready = true;
            return null;
        }
        try {
            ObjectNode result;
            if (method.equals("initialize")) {
                if (session != null && session.initialized) throw new ApiException(-32600, "protocol", "Already initialized");
                var params = request.path("params"); String requested = Json.text(params, "protocolVersion", 40);
                if (!params.path("capabilities").isObject() || !params.path("clientInfo").isObject()) throw new ApiException(-32602, "protocol", "Invalid initialization parameters");
                result = Json.obj().put("protocolVersion", VERSIONS.contains(requested) ? requested : VERSION);
                result.set("serverInfo", Json.obj().put("name", "voiceprint").put("version", "0.1.0"));
                result.set("capabilities", Json.obj().set("tools", Json.obj().put("listChanged", false)));
                result.put("instructions", "Speaker labels high/medium/low are similarity-based, not calibrated probabilities; OVERLAP lines are people talking at once and cannot be attributed. Null confidence is unavailable, not zero. Corrections are human labels; text comes from an external transcript source.");
                if (session != null) session.initialized = true;
            } else if (method.equals("ping")) result = Json.obj();
            else if (session != null && !session.ready) throw new ApiException(-32002, "protocol", "Complete initialization before calling tools");
            else if (method.equals("tools/list")) result = Json.obj().set("tools", tools());
            else if (method.equals("tools/call")) {
                var params = request.path("params"); String name = Json.text(params, "name", 80);
                if (!TOOLS.contains(name)) throw new ApiException(-32602, "protocol", "Unknown tool");
                JsonNode arguments = params.path("arguments");
                try { result = call(client, api, token, name, arguments, hosted, participant); }
                catch (Exception e) {
                    if (e instanceof InterruptedException) Thread.currentThread().interrupt();
                    String message = e instanceof ApiException ? e.getMessage() : "Voiceprint API is unavailable.";
                    result = toolResult(Json.error("tool_error", message), true);
                }
                result.put("label_kind", "similarity_based_uncalibrated").put("notice", LABEL_NOTICE);
                ((ArrayNode) result.withArray("content")).add(Json.obj().put("type", "text").put("text", LABEL_NOTICE));
            } else throw new ApiException(-32601, "protocol", "Method not found");
            var response = Json.obj().put("jsonrpc", "2.0"); response.set("id", id); response.set("result", result); return response;
        } catch (ApiException e) { return error(id, e.status < 0 ? e.status : -32602, e.getMessage()); }
    }
    private static ObjectNode call(HttpClient client, String base, String token, String tool, JsonNode args, boolean hosted, String participant) throws Exception {
        String path; String body = null;
        if (tool.equals("list_sessions")) path = "/speaker/sessions?limit=" + (args.has("limit") ? Json.integer(args, "limit", 1, 200) : 10);
        else path = "/speaker/session/" + Json.id(args, "session_id");
        if (tool.equals("list_sessions")) {}
        else if (tool.equals("get_agent_channel")) {
            long after = args.has("after_id") ? Json.integer(args, "after_id", 0, Integer.MAX_VALUE) : 0;
            long limit = args.has("limit") ? Json.integer(args, "limit", 1, 200) : 100;
            path += "/agent_channel?after_id=" + after + "&limit=" + limit + "&tier=all";
        }
        else if (tool.equals("post_agent_channel")) {
            // The sender is the URL tag, never an argument: an agent cannot post as another participant by naming it.
            if (participant == null) throw new ApiException(400, "tool_error", "post_agent_channel requires ?participant_id on the MCP URL");
            String tier = args.has("tier") ? Json.text(args, "tier", 10) : "raw";
            if (!Set.of("board", "raw").contains(tier)) throw new ApiException(-32602, "protocol", "tier must be board or raw");
            path += "/agent_channel";
            body = Json.obj().put("sender_participant_id", participant).put("tier", tier).put("text", Json.text(args, "text", 4000)).toString();
        }
        else if (tool.equals("get_transcript")) {
            long after = args.has("after_id") ? Json.integer(args, "after_id", 0, Integer.MAX_VALUE) : 0;
            long limit = args.has("limit") ? Json.integer(args, "limit", 1, 200) : 100;
            path += "/utterances?after_id=" + after + "&limit=" + limit;
            if (args.has("min_label")) {
                String minLabel = Json.text(args, "min_label", 10);
                if (!Set.of("high", "medium", "low").contains(minLabel)) throw new ApiException(-32602, "protocol", "min_label must be high, medium or low");
                path += "&min_label=" + minLabel;
            }
        }
        else if (tool.equals("get_current_speaker")) path += "/current";
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
        if (hosted) builder.header("X-Voiceprint-Hosted-MCP", "true");
        if (body != null) builder.header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(body));
        var response = client.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        JsonNode value = Json.parse(response.body());
        if (tool.equals("get_transcript") && response.statusCode() < 400) {
            // Token-cheap: plain lines for the model, cursor kept in structuredContent.
            String text = value.path("text").asText().strip();
            // Some clients surface only structuredContent, so the lines live there as well.
            var compact = Json.obj().put("session_id", value.path("session_id").asText()).put("next_after_id", value.path("next_after_id").asLong()).put("count", value.path("utterances").size())
                .put("transcript", text.isEmpty() ? "(no utterances yet)" : text);
            var result = Json.obj().put("isError", false); result.set("structuredContent", compact);
            result.putArray("content").add(Json.obj().put("type", "text").put("text", text.isEmpty() ? "(no utterances yet)" : text.strip()));
            return result;
        }
        if (tool.equals("get_agent_channel") && response.statusCode() < 400) {
            String text = value.path("text").asText().strip();
            var compact = Json.obj().put("session_id", value.path("session_id").asText()).put("next_after_id", value.path("next_after_id").asLong()).put("count", value.path("rows").size())
                .put("channel", text.isEmpty() ? "(no channel messages yet)" : text);
            var result = Json.obj().put("isError", false); result.set("structuredContent", compact);
            result.putArray("content").add(Json.obj().put("type", "text").put("text", compact.path("channel").asText()));
            return result;
        }
        if (tool.equals("post_agent_channel") && response.statusCode() < 400) {
            var compact = Json.obj().put("row_id", value.path("row_id").asLong()).put("tier", value.path("tier").asText()).put("redactions", value.path("redactions").asInt());
            return toolResult(compact, false);
        }
        if (tool.equals("list_sessions") && response.statusCode() < 400) {
            var lines = new StringBuilder();
            for (var s : value.path("sessions")) lines.append(s.path("session_id").asText()).append(' ').append(s.path("status").asText()).append(' ').append(s.path("elapsed_ms").asLong() / 1000).append("s [").append(s.path("participants").asText()).append("]\n");
            var result = Json.obj().put("isError", false); result.set("structuredContent", value);
            result.putArray("content").add(Json.obj().put("type", "text").put("text", lines.isEmpty() ? "(no sessions)" : lines.toString().strip()));
            return result;
        }
        return toolResult(value, response.statusCode() >= 400);
    }
    private static ObjectNode toolResult(JsonNode value, boolean failed) {
        var result = Json.obj().put("isError", failed); result.set("structuredContent", value);
        result.putArray("content").add(Json.obj().put("type", "text").put("text", value.toString())); return result;
    }
    private static ArrayNode tools() {
        var result = Json.arr();
        var sessions = tool("list_sessions", "List recent Voiceprint sessions (newest first) with status and enrolled participants. Call first to find a session_id.", true);
        ((ObjectNode) sessions.path("inputSchema").path("properties")).set("limit", Json.obj().put("type", "integer").put("minimum", 1).put("maximum", 200));
        result.add(sessions);
        var transcript = tool("get_transcript", "Get the attributed transcript as compact lines '#id m:ss.s-m:ss.s Name [label]: words'. Labels high/medium/low are similarity-based, NOT calibrated probabilities; 'OVERLAP A+B' lines are people talking over each other and their words cannot be attributed. Agent labels are declared by their registered producer. Pass after_id from the previous call to fetch only new lines; min_label=high returns high human labels and all agent lines.", true, "session_id");
        ObjectNode tp = (ObjectNode) transcript.path("inputSchema").path("properties");
        ObjectNode minLabel = Json.obj().put("type", "string");
        minLabel.putArray("enum").add("high").add("medium").add("low");
        tp.set("min_label", minLabel);
        tp.set("after_id", Json.obj().put("type", "integer").put("minimum", 0));
        tp.set("limit", Json.obj().put("type", "integer").put("minimum", 1).put("maximum", 200));
        result.add(transcript);
        result.add(tool("get_current_speaker", "Get current speaker, calibrated confidence when available, overlap and uncertainty.", true, "session_id"));
        var statements = tool("get_participant_statements", "Get a participant's statements with timestamps, attribution and confidence; text may be null without an ASR source.", true, "session_id", "speaker_id");
        ObjectNode properties = (ObjectNode) statements.path("inputSchema").path("properties");
        properties.set("after_sequence", Json.obj().put("type", "integer").put("minimum", -1));
        properties.set("limit", Json.obj().put("type", "integer").put("minimum", 1).put("maximum", 200));
        result.add(statements);
        result.add(tool("correct_attribution", "Apply a user-provided correction to an exact segment; update the profile only for verified single-speaker audio.", false, "session_id", "segment_id", "actual_speaker"));
        var channel = tool("get_agent_channel", "Read the agent-to-agent channel as compact lines '#id HH:MM:SS Sender [board|raw](TAG): text'. board rows are the neutral notes board visible to the humans; raw rows are private agent notes. Pass after_id from the previous call to fetch only new lines. Stored text has already passed the redaction guard.", true, "session_id");
        ObjectNode cp = (ObjectNode) channel.path("inputSchema").path("properties");
        cp.set("after_id", Json.obj().put("type", "integer").put("minimum", 0));
        cp.set("limit", Json.obj().put("type", "integer").put("minimum", 1).put("maximum", 200));
        result.add(channel);
        var post = tool("post_agent_channel", "Post a text note to the agent channel as the participant named on this MCP URL. tier=raw (default) is a private note for other agents; tier=board is the public notes board the humans see. Never state a participant's private constraint values; the server redacts literal matches as [withheld].", false, "session_id", "text");
        ObjectNode pp = (ObjectNode) post.path("inputSchema").path("properties");
        pp.set("text", Json.obj().put("type", "string").put("minLength", 1).put("maxLength", 4000));
        ObjectNode tier = Json.obj().put("type", "string"); tier.putArray("enum").add("board").add("raw");
        pp.set("tier", tier);
        result.add(post);
        return result;
    }
    private static ObjectNode tool(String name, String description, boolean readOnly, String... fields) {
        var result = Json.obj().put("name", name).put("description", description);
        var schema = Json.obj().put("type", "object").put("additionalProperties", false); var props = schema.putObject("properties"); var required = schema.putArray("required");
        for (String field : fields) { props.set(field, Json.obj().put("type", "string").put("pattern", "^[A-Za-z0-9_-]{1,80}$")); required.add(field); }
        result.set("inputSchema", schema);
        result.set("annotations", Json.obj().put("readOnlyHint", readOnly).put("destructiveHint", !readOnly).put("openWorldHint", false)); return result;
    }
    static ObjectNode error(JsonNode id, int code, String message) {
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
