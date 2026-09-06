package dev.voiceprint;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.*;

interface SpeechEngine {
    record Result(String modelId, double[] embedding, boolean speech, String overlap, boolean speakerChange, boolean profileEligible) {}
    record Candidate(String id, double similarity) {}
    record Match(List<Candidate> candidates, Double confidence, String calibrationId) {}
    Result analyze(byte[] pcm, boolean enrollment);
    Match match(Result result, List<Store.Profile> profiles);

    final class Remote implements SpeechEngine {
        private final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        private final URI uri;
        Remote(String url) { uri = URI.create(url + "/analyze"); }
        public Match match(Result result, List<Store.Profile> profiles) {
            var request = Json.obj().put("model_id", result.modelId());
            request.set("embedding", Json.MAPPER.valueToTree(result.embedding()));
            var participants = request.putArray("profiles");
            for (var p : profiles) participants.add(Json.obj().put("id", p.id()).set("embedding", Json.MAPPER.valueToTree(p.vector())));
            try {
                var response = client.send(HttpRequest.newBuilder(uri.resolve("/match")).timeout(Duration.ofSeconds(2))
                    .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(request.toString())).build(), HttpResponse.BodyHandlers.ofString());
                if (response.statusCode() != 200) throw new IllegalStateException();
                var n = Json.parse(response.body()); var candidates = new ArrayList<Candidate>(); var seen = new HashSet<String>();
                if (!n.path("candidates").isArray() || n.path("candidates").size() != profiles.size()) throw new IllegalStateException();
                double previous = 2;
                for (var c : n.path("candidates")) {
                    String id = Json.id(c, "speaker_id"); double score = c.path("similarity").asDouble(Double.NaN);
                    if (!seen.add(id) || profiles.stream().noneMatch(p -> p.id().equals(id)) || !Double.isFinite(score) || score < -1 || score > 1 || score > previous) throw new IllegalStateException();
                    candidates.add(new Candidate(id, score)); previous = score;
                }
                Double probability = n.path("confidence").isNull() ? null : n.path("confidence").asDouble(Double.NaN);
                if (probability != null && (!Double.isFinite(probability) || probability < 0 || probability > 1)) throw new IllegalStateException();
                String calibration = probability == null ? null : Json.text(n, "calibration_id", 100);
                return new Match(candidates, probability, calibration);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt(); throw new ApiException(503, "inference_unavailable", "Speaker matching was interrupted.");
            } catch (Exception e) { throw new ApiException(503, "inference_unavailable", "Speaker matching failed; retry the same sequence."); }
        }
        public Result analyze(byte[] pcm, boolean enrollment) {
            var request = Json.obj().put("audio_base64", Base64.getEncoder().encodeToString(pcm))
                .put("sample_rate", Audio.SAMPLE_RATE).put("enrollment", enrollment);
            try {
                var response = client.send(HttpRequest.newBuilder(uri).timeout(Duration.ofSeconds(enrollment ? 30 : 5))
                    .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(request.toString())).build(), HttpResponse.BodyHandlers.ofString());
                if (response.statusCode() != 200) throw new IllegalStateException("Worker returned an error");
                JsonNode n = Json.parse(response.body());
                String model = Json.text(n, "model_id", 256), overlap = Json.text(n, "overlap", 32);
                if (!Set.of("clear", "detected", "unavailable").contains(overlap)
                    || !n.path("speech").isBoolean() || !n.path("speaker_change").isBoolean() || !n.path("profile_eligible").isBoolean())
                    throw new IllegalStateException("Invalid worker response");
                double[] embedding = null;
                if (n.path("speech").asBoolean()) {
                    JsonNode values = n.path("embedding");
                    if (!values.isArray()) throw new IllegalStateException("Missing embedding");
                    embedding = new double[values.size()];
                    for (int i = 0; i < embedding.length; i++) {
                        if (!values.get(i).isNumber()) throw new IllegalStateException("Invalid embedding");
                        embedding[i] = values.get(i).doubleValue();
                    }
                    embedding = Audio.normalize(embedding);
                }
                return new Result(model, embedding, n.path("speech").asBoolean(), overlap, n.path("speaker_change").asBoolean(), n.path("profile_eligible").asBoolean());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new ApiException(503, "inference_unavailable", "Speech inference was interrupted; retry the same sequence.");
            } catch (Exception e) {
                throw new ApiException(503, "inference_unavailable", "Speech worker failed or timed out; retry the same sequence.");
            }
        }
    }
}
