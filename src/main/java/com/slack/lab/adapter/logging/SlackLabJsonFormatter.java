package com.slack.lab.adapter.logging;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.IThrowableProxy;
import ch.qos.logback.classic.spi.ThrowableProxyUtil;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.Map;
import org.springframework.boot.logging.structured.StructuredLogFormatter;
import org.springframework.core.env.Environment;

/**
 * 한 줄 JSON 로그(6단계 M44). 콘솔과 회전 파일이 같은 포매터를 쓰므로 "같은 JSON"이 구조적으로 보장된다.
 * {@code msg}는 한국어 원문을 이스케이프 없이 그대로 둔다 — 기존 로그 소비자(p1-metrics·n8n-watch)가 부분 문자열로 찾기 때문이다.
 */
public class SlackLabJsonFormatter implements StructuredLogFormatter<ILoggingEvent> {

    private static final DateTimeFormatter TS = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'")
            .withZone(ZoneOffset.UTC);

    private final String role;

    public SlackLabJsonFormatter(Environment environment) {
        this.role = environment.getProperty("app.role", "all");
    }

    @Override
    public String format(ILoggingEvent e) {
        String msg = e.getFormattedMessage();
        String event = EventKeys.eventOf(msg);
        StringBuilder sb = new StringBuilder(256);
        sb.append("{\"v\":1,\"ts\":\"").append(TS.format(Instant.ofEpochMilli(e.getTimeStamp())))
                .append("\",\"level\":\"").append(e.getLevel()).append("\",\"role\":");
        quote(sb, role);
        sb.append(",\"logger\":");
        quote(sb, e.getLoggerName());
        sb.append(",\"msg\":");
        quote(sb, msg);
        if (event != null) {
            sb.append(",\"event\":\"").append(event).append('"');
            Map<String, Object> fields = EventKeys.fieldsOf(event, msg);
            if (!fields.isEmpty()) {
                sb.append(",\"fields\":{");
                boolean first = true;
                for (var f : fields.entrySet()) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    sb.append('"').append(f.getKey()).append("\":");
                    if (f.getValue() instanceof String s) {
                        quote(sb, s);
                    } else {
                        sb.append(f.getValue());
                    }
                }
                sb.append('}');
            }
        }
        IThrowableProxy tp = e.getThrowableProxy();
        if (tp != null) {
            sb.append(",\"exc\":");
            quote(sb, tp.getClassName() + ": " + ThrowableProxyUtil.asString(tp));
        }
        return sb.append("}\n").toString();
    }

    /** JSON 문자열 이스케이프. 비ASCII는 그대로 두고 제어 문자만 이스케이프한다(개행도 \n이라 한 줄이 유지된다). */
    static void quote(StringBuilder sb, String s) {
        sb.append('"');
        if (s != null) {
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                switch (c) {
                    case '"' -> sb.append("\\\"");
                    case '\\' -> sb.append("\\\\");
                    case '\n' -> sb.append("\\n");
                    case '\r' -> sb.append("\\r");
                    case '\t' -> sb.append("\\t");
                    default -> {
                        if (c < 0x20 || c == 0x2028 || c == 0x2029) {
                            sb.append(String.format("\\u%04x", (int) c));
                        } else {
                            sb.append(c);
                        }
                    }
                }
            }
        }
        sb.append('"');
    }
}
