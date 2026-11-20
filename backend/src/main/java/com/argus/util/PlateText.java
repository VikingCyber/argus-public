package com.argus.util;

/**
 * Сведение номера к сравнимому виду.
 *
 * Номера в базе лежат кириллицей: vision нормализует распознанный текст
 * функцией normalize_plate() (vision/plate_text.py), переводя латинские
 * ABEKMHOPCTXY в АВЕКМНОРСТУХ. Но человек вводит номер в той раскладке,
 * в которой у него включена клавиатура, поэтому «A123BC 77» с английской
 * раскладки не совпадал с «А123ВС77» — поиск зависел от раскладки.
 *
 * Здесь обе формы приводятся к латинице только на время сравнения, данные
 * не переписываются. Разделители (пробелы, точки, дефисы) убираются:
 * на номера их ставят по-разному, а в базе их нет вовсе.
 */
public final class PlateText {

    private static final String CYRILLIC = "АВЕКМНОРСТУХ";
    private static final String LATIN = "ABEKMHOPCTYX";

    private PlateText() {
    }

    /** Кириллица -> латиница, верхний регистр, без разделителей. */
    public static String toLatin(String value) {
        if (value == null || value.isEmpty()) {
            return "";
        }
        String upper = value.toUpperCase();
        StringBuilder sb = new StringBuilder(upper.length());
        for (int i = 0; i < upper.length(); i++) {
            char c = upper.charAt(i);
            int idx = CYRILLIC.indexOf(c);
            sb.append(idx >= 0 ? LATIN.charAt(idx) : c);
        }
        return sb.toString();
    }

    /** Латиница -> кириллица, верхний регистр, без разделителей. */
    public static String toCyrillic(String value) {
        if (value == null || value.isEmpty()) {
            return "";
        }
        String upper = value.toUpperCase();
        StringBuilder sb = new StringBuilder(upper.length());
        for (int i = 0; i < upper.length(); i++) {
            char c = upper.charAt(i);
            int idx = LATIN.indexOf(c);
            sb.append(idx >= 0 ? CYRILLIC.charAt(idx) : c);
        }
        return sb.toString();
    }

    /**
     * Вид номера для сравнения с базой: латиница, без разделителей.
     * null на входе даёт null — чтобы «фильтр не задан» отличался от
     * «фильтр задан пустой строкой».
     */
    public static String normalizeSearch(String value) {
        if (value == null) {
            return null;
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (!Character.isWhitespace(c) && c != '.' && c != '-' && c != '_') {
                sb.append(c);
            }
        }
        return toLatin(sb.toString());
    }
}
