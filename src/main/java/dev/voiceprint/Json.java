package dev.voiceprint;

import com.fasterxml.jackson.databind.*;
import com.fasterxml.jackson.databind.node.*;
import java.io.IOException;

final class Json {
    static final ObjectMapper MAPPER = new ObjectMapper().enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
        .enable(com.fasterxml.jackson.core.JsonParser.Feature.STRICT_DUPLICATE_DETECTION);
    static ObjectNode obj() { return MAPPER.createObjectNode(); }
    static ArrayNode arr() { return MAPPER.createArrayNode(); }
    static JsonNode parse(String s) {
        try { return MAPPER.readTree(s); }
        catch (IOException e) { throw new ApiException(400, "invalid_json", "Expected a valid JSON document."); }
    }
    static String text(JsonNode n, String key, int max) {
        JsonNode v = n.path(key);
        if (!v.isTextual() || v.textValue().isBlank() || v.textValue().length() > max)
            throw new ApiException(400, "invalid_input", key + " must be a nonempty string of at most " + max + " characters.");
        return v.textValue();
    }
    static String id(JsonNode n, String key) {
        String value = text(n, key, 80);
        if (!value.matches("[A-Za-z0-9_-]+")) throw new ApiException(400, "invalid_input", key + " must contain letters, numbers, underscores or hyphens.");
        return value;
    }
    static long integer(JsonNode n, String key, long min, long max) {
        JsonNode v = n.path(key);
        if (!v.isIntegralNumber() || !v.canConvertToLong() || v.longValue() < min || v.longValue() > max)
            throw new ApiException(400, "invalid_input", key + " is outside its allowed integer range.");
        return v.longValue();
    }
    static ObjectNode error(String code, String message) { return obj().put("error", code).put("message", message); }
}
