package dev.voiceprint;

import java.nio.file.Path;
import java.time.Clock;

public final class Main {
    public static void main(String[] args) throws Exception {
        if (args.length > 0 && args[0].equals("mcp")) { McpServer.run(System.in, System.out, env("VOICEPRINT_API_URL", "http://127.0.0.1:8080"), System.getenv("VOICEPRINT_API_TOKEN")); return; }
        System.setProperty("sun.net.httpserver.maxReqTime", "10");
        var store = new Store(Path.of(env("VOICEPRINT_DB", "data/voiceprint.sqlite")));
        var service = new SpeakerService(store, new SpeechEngine.Remote(env("VOICEPRINT_WORKER_URL", "http://127.0.0.1:8091")), Clock.systemUTC());
        var server = new RestServer(service, Integer.parseInt(env("VOICEPRINT_PORT", "8080")), System.getenv("VOICEPRINT_API_TOKEN"));
        Runtime.getRuntime().addShutdownHook(new Thread(() -> { server.close(); try { store.close(); } catch (Exception ignored) {} }));
        server.start(); System.err.println("Voiceprint API listening on http://127.0.0.1:" + server.port());
    }
    static String env(String key, String fallback) { return System.getenv().getOrDefault(key, fallback); }
}
