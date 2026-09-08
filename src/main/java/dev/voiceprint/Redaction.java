package dev.voiceprint;

import java.util.*;
import java.util.regex.*;

/**
 * Server-authoritative literal-value guard (docs/API.md, V3 redaction guard). Registered numeric values match digit
 * or spelled-out mentions with the same canonical number; text values match as whole-word phrases. Not a classifier:
 * adjacent numbers, ranges, paraphrase and ordinals are documented misses. Shared vectors: evaluation/redaction_cases.json.
 */
final class Redaction {
    static final String TOKEN = "[withheld]";
    record Result(String text, int hits) {}
    private static final Map<String, Long> WORDS = new HashMap<>();
    private static final Map<String, Long> SCALES = Map.of("hundred", 100L, "thousand", 1000L, "million", 1_000_000L, "billion", 1_000_000_000L);
    static {
        String[] ones = {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"};
        for (int i = 0; i < ones.length; i++) WORDS.put(ones[i], (long) i);
        String[] tens = {"twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"};
        for (int i = 0; i < tens.length; i++) WORDS.put(tens[i], 20L + 10L * i);
    }
    private static final Pattern DIGITS = Pattern.compile("(?<![\\p{L}\\p{N}])(?:\\$ ?)?(\\d{1,3}(?:,\\d{3})+|\\d+)(\\.\\d+)?(?: ?(k|m|thousand|million|billion|%))?(?![\\p{L}\\p{N}])", Pattern.CASE_INSENSITIVE);
    private static final Pattern WORD = Pattern.compile("\\p{L}+");
    private static final Pattern VALUE = Pattern.compile("(\\d+(?:\\.\\d+)?|\\.\\d+)(k|m|thousand|million)?", Pattern.CASE_INSENSITIVE);

    static Result redact(String text, Collection<String> values) {
        if (text == null || text.isEmpty() || values == null || values.isEmpty()) return new Result(text == null ? "" : text, 0);
        var numbers = new ArrayList<Double>(); var phrases = new ArrayList<Pattern>();
        for (String value : values) {
            if (value == null) continue;
            Double number = number(value);
            if (number != null) { if (numbers.stream().noneMatch(n -> same(n, number))) numbers.add(number); continue; }
            String[] words = value.trim().toLowerCase(Locale.ROOT).split("[^\\p{L}\\p{N}]+");
            var parts = Arrays.stream(words).filter(w -> !w.isEmpty()).toList();
            if (String.join(" ", parts).length() < 3) continue;
            phrases.add(Pattern.compile("(?<![\\p{L}\\p{N}])" + String.join("[\\s\\p{Punct}]+", parts.stream().map(Pattern::quote).toList()) + "(?![\\p{L}\\p{N}])", Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE));
        }
        int hits = 0;
        if (!numbers.isEmpty()) {
            var out = new StringBuilder(); var m = DIGITS.matcher(text); int last = 0;
            while (m.find()) {
                double mention = Double.parseDouble(m.group(1).replace(",", "") + (m.group(2) == null ? "" : m.group(2))) * multiplier(m.group(3));
                if (numbers.stream().noneMatch(n -> same(n, mention))) continue;
                out.append(text, last, m.start()).append(TOKEN); last = m.end(); hits++;
            }
            text = out.append(text, last, text.length()).toString();
            var spelled = spelled(text, numbers); text = spelled.text; hits += spelled.hits;
        }
        for (Pattern phrase : phrases) {
            var out = new StringBuilder(); var m = phrase.matcher(text); int last = 0;
            while (m.find()) { out.append(text, last, m.start()).append(TOKEN); last = m.end(); hits++; }
            text = out.append(text, last, text.length()).toString();
        }
        return new Result(text, hits);
    }
    /** Rule 1: strip $ , _ spaces %, apply a trailing multiplier, and parse; null means a text value. */
    static Double number(String value) {
        var m = VALUE.matcher(value.replaceAll("[$,_\\s%]", ""));
        return m.matches() ? Double.parseDouble(m.group(1)) * multiplier(m.group(2)) : null;
    }
    private static double multiplier(String suffix) {
        if (suffix == null) return 1;
        return switch (suffix.toLowerCase(Locale.ROOT)) { case "k", "thousand" -> 1000; case "m", "million" -> 1_000_000; case "billion" -> 1_000_000_000; default -> 1; };
    }
    private static boolean same(double a, double b) { return Math.abs(a - b) < 0.005; }
    /** Rule 2, spelled-out form: maximal runs of number words joined by whitespace or hyphens; "and" only between parts. */
    private static Result spelled(String text, List<Double> numbers) {
        var out = new StringBuilder(); int last = 0, hits = 0;
        var m = WORD.matcher(text);
        var words = new ArrayList<int[]>(); var lower = new ArrayList<String>();
        while (m.find()) { words.add(new int[] {m.start(), m.end()}); lower.add(m.group().toLowerCase(Locale.ROOT)); }
        for (int i = 0; i < words.size(); ) {
            if (!numberWord(lower.get(i)) || lower.get(i).equals("and")) { i++; continue; }
            // A scale word right after a digit mention belongs to that mention, not to a new spelled span.
            if (SCALES.containsKey(lower.get(i)) && precededByDigit(text, words.get(i)[0])) { i++; continue; }
            int end = i;
            while (end + 1 < words.size() && numberWord(lower.get(end + 1)) && text.substring(words.get(end)[1], words.get(end + 1)[0]).matches("[\\s-]+")) end++;
            while (end > i && lower.get(end).equals("and")) end--;
            double value = value(lower.subList(i, end + 1));
            int start = words.get(i)[0], stop = words.get(end)[1];
            if (numbers.stream().anyMatch(n -> same(n, value))) { out.append(text, last, start).append(TOKEN); last = stop; hits++; }
            i = end + 1;
        }
        return new Result(out.append(text, last, text.length()).toString(), hits);
    }
    private static boolean precededByDigit(String text, int start) {
        int i = start - 1;
        while (i >= 0 && text.charAt(i) == ' ') i--;
        return i >= 0 && Character.isDigit(text.charAt(i));
    }
    private static boolean numberWord(String word) { return WORDS.containsKey(word) || SCALES.containsKey(word) || word.equals("and"); }
    private static double value(List<String> words) {
        double total = 0, current = 0;
        for (String word : words) {
            if (word.equals("and")) continue;
            Long scale = SCALES.get(word);
            if (scale == null) current += WORDS.get(word);
            else if (scale == 100) current = (current == 0 ? 1 : current) * 100;
            else { total += (current == 0 ? 1 : current) * scale; current = 0; }
        }
        return total + current;
    }
}
