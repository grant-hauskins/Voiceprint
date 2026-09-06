package dev.voiceprint;

import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import java.io.*;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import static org.junit.jupiter.api.Assertions.*;

class TransportTest {
    @TempDir Path temp;
    Store store; RestServer server; String base;
    @BeforeEach void setup() throws Exception {
        store = new Store(temp.resolve("transport.sqlite"));
        var service = new SpeakerService(store, new SpeakerServiceTest.FakeEngine(), new SpeakerServiceTest.MutableClock());
        service.init(SpeakerServiceTest.initRequest("test"));
        server = new RestServer(service, 0, "test-secret"); server.start(); base = "http://127.0.0.1:" + server.port();
    }
    @AfterEach void close() throws Exception { server.close(); store.close(); }
    @Test void restEnforcesTokenAndInputValidation() throws Exception {
        var client = HttpClient.newHttpClient();
        assertEquals(401, client.send(HttpRequest.newBuilder(URI.create(base + "/health")).build(), HttpResponse.BodyHandlers.ofString()).statusCode());
        var malformed = HttpRequest.newBuilder(URI.create(base + "/speaker/session/test/audio")).header("Authorization", "Bearer test-secret")
            .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString("{broken"));
        assertEquals(400, client.send(malformed.build(), HttpResponse.BodyHandlers.ofString()).statusCode());
        var origin = HttpRequest.newBuilder(URI.create(base + "/health")).header("Authorization", "Bearer test-secret").header("Origin", "https://example.org");
        assertEquals(403, client.send(origin.build(), HttpResponse.BodyHandlers.ofString()).statusCode());
    }
    @Test void mcpHandshakeToolsAndRealHttpQuery() throws Exception {
        String input = """
            {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}
            {"jsonrpc":"2.0","method":"notifications/initialized"}
            {"jsonrpc":"2.0","id":2,"method":"tools/list"}
            {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_current_speaker","arguments":{"session_id":"test"}}}
            {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_current_speaker","arguments":{"session_id":"missing"}}}
            """;
        var output = new ByteArrayOutputStream(); McpServer.run(new ByteArrayInputStream(input.getBytes(StandardCharsets.UTF_8)), output, base, "test-secret");
        var lines = output.toString(StandardCharsets.UTF_8).lines().map(Json::parse).toList();
        assertEquals(4, lines.size());
        assertEquals("2025-11-25", lines.get(0).path("result").path("protocolVersion").asText());
        assertEquals(5, lines.get(1).path("result").path("tools").size());
        assertEquals("waiting", lines.get(2).path("result").path("structuredContent").path("status").asText());
        assertTrue(lines.get(3).path("result").path("isError").asBoolean());
    }
    @Test void mcpRequiresInitializationAndRecoversFromMalformedJson() throws Exception {
        var output = new ByteArrayOutputStream();
        String input = "broken\n{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}\n";
        McpServer.run(new ByteArrayInputStream(input.getBytes(StandardCharsets.UTF_8)), output, base, null);
        var lines = output.toString(StandardCharsets.UTF_8).lines().map(Json::parse).toList();
        assertEquals(-32700, lines.get(0).path("error").path("code").asInt());
        assertEquals(-32002, lines.get(1).path("error").path("code").asInt());
    }
}
