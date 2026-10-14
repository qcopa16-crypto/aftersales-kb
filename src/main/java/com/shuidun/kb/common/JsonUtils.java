package com.shuidun.kb.common;

import java.util.ArrayList;
import java.util.List;
import tools.jackson.core.type.TypeReference;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/**
 * 轻量 JSON 工具：用于对前端载荷（camelCase）与日志 JSON 字段的序列化。
 *
 * <p>不注入 Spring 的 ObjectMapper，避免与 AI 契约用的 snake_case 映射器产生歧义。
 */
public final class JsonUtils {

    private static final ObjectMapper MAPPER = JsonMapper.builder().build();

    private JsonUtils() {
    }

    /**
     * 序列化为 JSON 字符串。
     *
     * @param value 对象
     * @return JSON 字符串；失败返回 null
     */
    public static String toJson(Object value) {
        if (value == null) {
            return null;
        }
        try {
            return MAPPER.writeValueAsString(value);
        } catch (RuntimeException ex) {
            return null;
        }
    }

    /**
     * 反序列化字符串数组；空值或非法 JSON 返回空列表。
     *
     * @param json JSON 数组字符串
     * @return 字符串列表
     */
    public static List<String> toStringList(String json) {
        return readList(json, new TypeReference<List<String>>() {
        });
    }

    /**
     * 反序列化 id 数组（日志里的 unit id JSON 字段）；空值或非法 JSON 返回空列表。
     *
     * @param json JSON 数组字符串
     * @return id 列表
     */
    public static List<Long> toLongList(String json) {
        return readList(json, new TypeReference<List<Long>>() {
        });
    }

    /**
     * 读取 JSON 对象中指定字段的字符串数组；缺失/非数组/非法 JSON 返回空列表。
     *
     * @param json  JSON 对象字符串
     * @param field 字段名
     * @return 字符串列表
     */
    public static List<String> fieldStringList(String json, String field) {
        if (json == null || json.isBlank() || field == null) {
            return new ArrayList<>();
        }
        try {
            JsonNode node = MAPPER.readTree(json).get(field);
            if (node == null || !node.isArray()) {
                return new ArrayList<>();
            }
            List<String> out = new ArrayList<>();
            for (JsonNode item : node) {
                out.add(item.asText());
            }
            return out;
        } catch (RuntimeException ex) {
            return new ArrayList<>();
        }
    }

    /**
     * 读取 JSON 对象中指定字段的字符串值；缺失/非标量/非法 JSON 返回 null。
     *
     * @param json  JSON 对象字符串
     * @param field 字段名
     * @return 字段文本；不存在或非标量返回 null
     */
    public static String fieldString(String json, String field) {
        if (json == null || json.isBlank() || field == null) {
            return null;
        }
        try {
            JsonNode root = MAPPER.readTree(json);
            if (root == null) {
                return null;
            }
            JsonNode node = root.get(field);
            if (node == null || node.isNull() || !node.isValueNode()) {
                return null;
            }
            return node.asText();
        } catch (RuntimeException ex) {
            return null;
        }
    }

    private static <T> List<T> readList(String json, TypeReference<List<T>> type) {
        if (json == null || json.isBlank()) {
            return new ArrayList<>();
        }
        try {
            return MAPPER.readValue(json, type);
        } catch (RuntimeException ex) {
            return new ArrayList<>();
        }
    }
}
