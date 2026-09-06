package dev.voiceprint;

import java.nio.file.Path;
import java.time.Clock;
import java.util.concurrent.*;

public final class Main {
    public static void main(String[] args) throws Exception {
        if (args.length > 0 && args[0].equals("mcp")) { McpServer.run(System.in, System.out, env("VOICEPRINT_API_URL", "http://127.0.0.1:8080"), System.getenv("VOICEPRINT_API_TOKEN")); return; }
        System.setProperty("sun.net.httpserver.maxReqTime", "10");
        var store = new Store(Path.of(env("VOICEPRINT_DB", "data/voiceprint.sqlite")));
        var service = new SpeakerService(store, new SpeechEngine.Remote(env("VOICEPRINT_WORKER_URL", "http://127.0.0.1:8091")), Clock.systemUTC());
        var server = new RestServer(service, Integer.parseInt(env("VOICEPRINT_PORT", "8080")), System.getenv("VOICEPRINT_API_TOKEN"));
        var mcp = new McpHttpServer(Integer.parseInt(env("VOICEPRINT_MCP_PORT", "8082")), "http://127.0.0.1:" + server.port(), System.getenv("VOICEPRINT_API_TOKEN"), System.getenv("VOICEPRINT_MCP_TOKEN"), service);
        var sweeper = Executors.newSingleThreadScheduledExecutor();
        Runnable sweep = () -> { try { service.sweepPrivacy(); } catch (Exception e) { System.err.println("Privacy destruction retry needed: " + e.getClass().getSimpleName()); } };
        service.setDestructionWakeup(() -> sweeper.execute(sweep));
        sweeper.scheduleWithFixedDelay(sweep, 0, 60, TimeUnit.SECONDS);
        Runtime.getRuntime().addShutdownHook(new Thread(() -> { sweeper.shutdownNow(); mcp.close(); server.close(); synchronized (service) { try { store.close(); } catch (Exception ignored) {} } }));
        server.start(); System.err.println("Voiceprint API listening on http://127.0.0.1:" + server.port());
        mcp.start(); System.err.println("Voiceprint MCP (Streamable HTTP) listening on http://127.0.0.1:" + mcp.port() + "/mcp" + (System.getenv("VOICEPRINT_MCP_TOKEN") == null ? "  (no VOICEPRINT_MCP_TOKEN set: anyone reaching this port can read transcripts)" : ""));
    }
    static String env(String key, String fallback) { return System.getenv().getOrDefault(key, fallback); }
}
