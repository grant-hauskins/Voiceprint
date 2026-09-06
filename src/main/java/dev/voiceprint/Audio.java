package dev.voiceprint;

import java.util.*;

final class Audio {
    static final int SAMPLE_RATE = 16000, CHUNK_BYTES = 8000, CONTEXT_BYTES = 48000;
    static byte[] decode(String encoded, boolean enrollment) {
        byte[] bytes;
        try { bytes = Base64.getDecoder().decode(encoded); }
        catch (IllegalArgumentException e) { throw new ApiException(400, "invalid_audio", "Audio must be base64-encoded PCM."); }
        if (bytes.length % 2 != 0 || (enrollment ? bytes.length < 160000 || bytes.length > 480000 : bytes.length != CHUNK_BYTES))
            throw new ApiException(400, "invalid_audio", enrollment ? "Enrollment requires 5–15 seconds of mono 16 kHz PCM16LE." : "Each live chunk must contain exactly 250 ms of mono 16 kHz PCM16LE (8000 bytes).");
        if (bytes.length >= 4 && bytes[0] == 'R' && bytes[1] == 'I' && bytes[2] == 'F' && bytes[3] == 'F')
            throw new ApiException(400, "invalid_audio", "Send raw PCM samples, without a WAV header.");
        return bytes;
    }
    static byte[] append(byte[] previous, byte[] next) {
        int retained = Math.min(previous.length, CONTEXT_BYTES - next.length);
        byte[] result = new byte[retained + next.length];
        System.arraycopy(previous, previous.length - retained, result, 0, retained);
        System.arraycopy(next, 0, result, retained, next.length);
        return result;
    }
    static double[] normalize(double[] vector) {
        if (vector == null || vector.length < 2 || vector.length > 4096) throw new IllegalArgumentException("Invalid embedding dimensions");
        double norm = 0;
        for (double v : vector) { if (!Double.isFinite(v)) throw new IllegalArgumentException("Non-finite embedding"); norm += v * v; }
        if (!Double.isFinite(norm) || norm < 1e-12) throw new IllegalArgumentException("Empty embedding");
        double[] result = vector.clone();
        for (int i = 0; i < result.length; i++) result[i] /= Math.sqrt(norm);
        return result;
    }
    static double cosine(double[] a, double[] b) {
        if (a.length != b.length) throw new ApiException(503, "model_mismatch", "The worker embedding does not match the enrolled model.");
        double sum = 0; for (int i = 0; i < a.length; i++) sum += a[i] * b[i];
        return Math.max(-1, Math.min(1, sum));
    }
}
