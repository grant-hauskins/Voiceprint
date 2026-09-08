package dev.voiceprint;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

/** Synthetic people and synthetic PCM only; all tests use the real challenge/release flow. */
final class PrivacyTestSupport {
    static final PrivacyPolicy POLICY = new PrivacyPolicy("Synthetic Test Operator", "1 Test Street", "operator@example.invalid", "test-secret", true, true);
    static ObjectNode init(SpeakerService service, JsonNode request) {
        String id = request.path("session_id").asText();
        try { service.consentStatus(id); }
        catch (ApiException e) {
            if (e.status != 404) throw e;
            authorize(service, id, request.path("participants"), true);
        }
        return service.init(request);
    }
    static void authorize(SpeakerService service, String id, JsonNode roster, boolean disclosures) {
        authorize(service, id, roster, disclosures ? ALL_SCOPES : java.util.List.of());
    }
    static void authorize(SpeakerService service, String id, JsonNode roster, java.util.List<String> scopes) {
        var room = Json.obj().put("session_id", id).put("purpose_id", PrivacyPolicy.PURPOSE);
        var members = room.putArray("participants");
        for (var p : roster) members.add(Json.obj().put("id", p.path("id").asText()).put("name", p.path("name").asText()).put("contact", p.path("id").asText() + "@example.invalid"));
        service.createPrivacyRoom(room);
        for (var p : roster) sign(service, id, p.path("id").asText(), p.path("name").asText(), scopes);
    }
    static final java.util.List<String> ALL_SCOPES = java.util.List.of("openai_audio", "hosted_mcp", "negotiation_text");
    static final java.util.List<String> V2_SCOPES = java.util.List.of("openai_audio", "hosted_mcp");
    static void sign(SpeakerService service, String session, String id, String name, boolean disclosures) {
        sign(service, session, id, name, disclosures ? ALL_SCOPES : java.util.List.of());
    }
    static void sign(SpeakerService service, String session, String id, String name, java.util.List<String> scopes) {
        var challenge = service.consentChallenge(session, id);
        var release = Json.obj().put("challenge", challenge.path("challenge").asText()).put("notice_sha256", challenge.path("notice_sha256").asText()).put("signature_text", name).put("accepted", true);
        var selected = release.putArray("disclosure_scopes");
        for (String scope : scopes) selected.add(scope);
        service.consentRelease(session, id, release);
    }
}
