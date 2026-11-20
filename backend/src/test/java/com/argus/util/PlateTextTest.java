package com.argus.util;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Поиск по номеру не должен зависеть от раскладки клавиатуры.
 * В базе номера лежат кириллицей (vision/plate_text.py нормализует OCR в
 * АВЕКМНОРСТУХ), а человек вводит в любой раскладке.
 *
 * Тестовые данные записаны литеральной кириллицей, и это ловушка: в А, С и Е
 * легко поставить латинскую букву при наборе, и тогда проверка translate()
 * превратится в проверку "строка равна сама себе" и пройдёт вхолостую.
 * От такой подмены страхует testDataUsesDistinctAlphabets в конце файла.
 */
class PlateTextTest {

    /** С833СС338 кириллицей. */
    private static final String CYR_S = "С833СС338";
    /** C833CC338 латиницей — то же самое, что CYR_S. */
    private static final String LAT_S = "C833CC338";
    private static final String CYR_O = "О733";
    private static final String LAT_O = "O733";

    @Test
    void convertsCyrillicPlateToLatin() {
        assertEquals(LAT_S, PlateText.toLatin(CYR_S));
        assertEquals(LAT_O, PlateText.toLatin(CYR_O));
        assertEquals("C20874", PlateText.toLatin("С20874"));
    }

    @Test
    void convertsLatinPlateToCyrillic() {
        assertEquals(CYR_S, PlateText.toCyrillic(LAT_S));
        assertEquals(CYR_O, PlateText.toCyrillic(LAT_O));
    }

    @Test
    void bothLayoutsProduceTheSameSearchKey() {
        String typedCyrillic = PlateText.normalizeSearch("с833 сс338");
        String typedLatin = PlateText.normalizeSearch("c833 cc338");
        assertEquals(typedCyrillic, typedLatin);
        assertEquals(LAT_S, typedLatin);
    }

    @Test
    void stripsSeparatorsUsedInRealPlates() {
        assertEquals("A001AA77", PlateText.normalizeSearch("А001АА 77"));
        assertEquals("A001AA77", PlateText.normalizeSearch("а001аа-77"));
        assertEquals("A001AA77", PlateText.normalizeSearch("A001AA.77"));
        assertEquals("A001AA77", PlateText.normalizeSearch("a001aa_77"));
    }

    @Test
    void isCaseInsensitive() {
        assertEquals(LAT_S, PlateText.normalizeSearch(LAT_S.toLowerCase()));
        assertEquals(LAT_S, PlateText.normalizeSearch(CYR_S.toLowerCase()));
    }

    /**
     * Меняются только буквы-двойники. У Л нет латинского аналога, поэтому
     * она остаётся кириллицей — иначе номер с Л искался бы только при
     * кириллическом вводе, то есть исходный баг вернулся бы с другой буквой.
     */
    @Test
    void keepsLettersWithoutLatinLookalike() {
        String withL = "Л999";
        assertEquals(withL, PlateText.toLatin(withL));
        assertEquals(withL, PlateText.normalizeSearch(withL));
    }

    @Test
    void translatesOnlyTheLettersThatHaveLatinLookalikes() {
        assertEquals("X775YTT99", PlateText.toLatin("Х775УТТ99"));
        assertEquals("E001EO77", PlateText.toLatin("Е001ЕО77"));
    }

    @Test
    void nullQueryMeansNoFilter() {
        assertNull(PlateText.normalizeSearch(null));
        assertEquals("", PlateText.toLatin(null));
        assertEquals("", PlateText.toCyrillic(""));
    }

    /** Страховка от подмены кириллицы латиницей в самих тестовых данных. */
    @Test
    void testDataUsesDistinctAlphabets() {
        assertTrue(CYR_S.chars().anyMatch(c -> c > 127), "CYR_S должен содержать кириллицу");
        assertTrue(LAT_S.chars().allMatch(c -> c < 128), "LAT_S должен быть чисто латинским");
        assertTrue(CYR_O.chars().anyMatch(c -> c > 127), "CYR_O должен содержать кириллицу");
        assertTrue(LAT_O.chars().allMatch(c -> c < 128), "LAT_O должен быть чисто латинским");
    }
}
