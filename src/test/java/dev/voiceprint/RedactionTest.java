package dev.voiceprint;

import org.junit.jupiter.api.Test;
import java.nio.file.*;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

/** Every shared vector in evaluation/redaction_cases.json must pass exactly; the Python mirror runs the same file. */
class RedactionTest {
    @Test void everySharedVectorRedactsExactly() throws Exception {
        var fixture = Json.parse(Files.readString(Path.of("evaluation", "redaction_cases.json")));
        assertEquals(Redaction.TOKEN, fixture.path("token").asText());
        int cases = 0;
        for (var c : fixture.path("cases")) {
            var values = new ArrayList<String>();
            for (var v : c.path("values")) values.add(v.asText());
            var result = Redaction.redact(c.path("text").asText(), values);
            assertEquals(c.path("expected").asText(), result.text(), c.path("name").asText());
            assertEquals(c.path("hits").asInt(), result.hits(), c.path("name").asText());
            cases++;
        }
        assertTrue(cases >= 20, "fixture should hold the shared vectors");
    }
    @Test void numericClassificationFollowsRuleOne() {
        assertEquals(300000, Redaction.number("$300,000"));
        assertEquals(300000, Redaction.number("300 k"));
        assertEquals(1500000, Redaction.number("1.5M"));
        assertEquals(2000000, Redaction.number("2 million"));
        assertEquals(4.5, Redaction.number("4.5%"));
        assertNull(Redaction.number("June 30"));
        assertNull(Redaction.number("1e5"));
        assertNull(Redaction.number(""));
    }
}
