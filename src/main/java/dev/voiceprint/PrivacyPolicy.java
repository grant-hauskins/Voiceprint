package dev.voiceprint;

import com.fasterxml.jackson.databind.node.ObjectNode;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.util.*;

/** Operator configuration is an attestation, never a verified identity or legal certification. */
record PrivacyPolicy(String controllerName, String controllerAddress, String controllerEmail, String apiToken,
                     boolean openaiReviewed, boolean cloudflareReviewed, List<String> providers) {
    static final String VERSION = "voiceprint-live-retention-v1";
    static final String METHOD = "local-kiosk-checkbox-name-v1";
    static final String PURPOSE = "live_conversation_v1";
    static final String PURPOSE_TEXT = "identify consenting speakers and provide a speaker-attributed transcript during the current room conversation";
    static final long INACTIVITY_MS = 30 * 60 * 1000L;
    static final long SUMMARY_RETENTION_MS = 30L * 24 * 60 * 60 * 1000;
    static PrivacyPolicy environment() {
        return new PrivacyPolicy(System.getenv("VOICEPRINT_CONTROLLER_NAME"), System.getenv("VOICEPRINT_CONTROLLER_ADDRESS"),
            System.getenv("VOICEPRINT_CONTROLLER_EMAIL"), System.getenv("VOICEPRINT_API_TOKEN"),
            "true".equals(System.getenv("VOICEPRINT_OPENAI_REVIEWED")), "true".equals(System.getenv("VOICEPRINT_CLOUDFLARE_REVIEWED")), providers(System.getenv("VOICEPRINT_PROVIDERS")));
    }
    /** Comma-separated display names of the configured AI providers; rendered into the notice so its hash tracks the list. */
    static List<String> providers(String configured) {
        var names = new ArrayList<String>();
        if (configured != null) for (String name : configured.split(",")) if (!name.isBlank()) names.add(name.strip());
        return names.isEmpty() ? List.of("OpenAI") : List.copyOf(names);
    }
    private static boolean nonempty(String text) { return text != null && !text.isBlank(); }
    boolean configured() { return nonempty(controllerName) && nonempty(controllerAddress) && nonempty(controllerEmail) && nonempty(apiToken); }
    void requireConfigured() { if (!configured()) throw new ApiException(503, "privacy_configuration_required", "Configure the controller name, address, email and operator API token before collecting releases."); }
    String operatorIdentity() { requireConfigured(); return "api-credential-sha256:" + sha256(apiToken.getBytes(StandardCharsets.UTF_8)); }
    String retentionText() {
        return "Biometric payloads are used only for the current live conversation. Purpose completion or any withdrawal immediately stops access and starts destruction of the whole room. An abandoned room expires after 30 minutes without each participant's meaningful interaction; polling and agent replies do not extend it. The 60-second recovery sweeper retries failed destruction. A job remains incomplete while any destination lacks verified deletion. "
            + "A retained voiceprint (optional voice_profile_retention, chosen per person) is a separate retention class holding only that person's voice embedding, never audio or transcript; it is kept until the person deletes it in the console or three years after their last session (a February 29 deadline falls back to February 28), whichever comes first, a withdrawal from any room deletes it, and room destruction does not touch it. "
            + "Minimal non-biometric signature evidence is separate and its legal-evidence schedule requires operator approval. SQLite clearing does not prove erasure of SSD blocks, backups or OS snapshots. The controller must publish this policy publicly and review its legal sufficiency before collection.";
    }
    static final String RETENTION_SENTENCE = "Optional voice_profile_retention keeps your voiceprint (the enrollment embedding and its corrected updates, never audio or transcript) after this room ends so that later rooms you join start from it and refine it; it is kept until you withdraw it in the console or three years after your last session, whichever comes first, and it is deleted when you withdraw from a room. ";
    private String recipients() {
        return "the AI provider(s) the operator has configured and reviewed (currently " + String.join(", ", providers) + " through Cloudflare's tunnel for hosted MCP; the operator may configure other providers such as xAI or Google Gemini under the same release)";
    }
    String noticeText() {
        return "Voiceprint electronic written release\nController: " + Objects.toString(controllerName, "[not configured]") + "\nAddress: " + Objects.toString(controllerAddress, "[not configured]")
            + "\nContact: " + Objects.toString(controllerEmail, "[not configured]") + "\nPurpose: " + PURPOSE_TEXT + ".\n"
            + "Voiceprint captures enrollment and room audio, creates speaker-identifying voice embeddings, and stores attributed transcript, attribution, correction and tool-use records. "
            + "Local models process these data on the operator's computer. Voiceprints and related data are not sold, leased, traded or used for profit from biometric data. "
            + "Optional openai_audio disclosure sends room audio, and optional hosted_mcp disclosure sends attributed transcript and tool results, to " + recipients() + ". "
            + "The openai_ prefix in scope identifiers is historical; the recipients are the providers named above. "
            + "Optional negotiation_text disclosure stores each participant's typed negotiation objective (position and private constraint values), injects it only into that participant's own advocate agent, sends both objectives, the notes board and the named transcript to the text models of the providers named above for a neutral arbitrator and an end-of-conversation summary, and retains that written summary for 30 days after the room ends; private constraint values are never written to any shared channel. "
            + RETENTION_SENTENCE
            + "Each optional disclosure requires every participant's release and operator review of the recipient's agreements and account retention/deletion settings. Closing a provider connection does not prove deletion of its logs. "
            + retentionText() + "\nBy personally checking the release box, typing my full name and submitting, I affirmatively authorize the stated collection and local processing for this current conversation, and only the optional disclosures I select. "
            + "I may withdraw through the room controls or ask the operator to stop. I am signing for myself; representative consent is not supported. "
            + "The operator entered my name and contact; contact and participant identity are unverified. The operator's login does not grant my consent. "
            + "No microphone opens until every participant has submitted this written release. The subsequent spoken opening corroborates this earlier release and is not how prior consent is obtained. "
            + "Policy: " + VERSION + "; method: " + METHOD + ".";
    }
    String noticeHash() { return sha256(noticeText().getBytes(StandardCharsets.UTF_8)); }
    ObjectNode notice() {
        var n = Json.obj().put("configured", configured()).put("controller_name", controllerName).put("controller_address", controllerAddress).put("controller_email", controllerEmail)
            .put("policy_version", VERSION).put("consent_method_version", METHOD).put("purpose_id", PURPOSE).put("notice_text", noticeText()).put("notice_sha256", noticeHash()).put("retention_text", retentionText());
        n.set("vendors", Json.obj().put("openai_reviewed", openaiReviewed).put("cloudflare_reviewed", cloudflareReviewed));
        var names = n.putArray("providers"); for (String provider : providers) names.add(provider);
        return n;
    }
    static String sha256(byte[] bytes) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)); }
        catch (NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }
}
