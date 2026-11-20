package com.argus.service;

/**
 * Нормализация гос. номера для сравнения с белым списком:
 * верхний регистр, удаление пробелов/дефисов/точек.
 * «а 777 аа 77» и «А777АА77» дают одинаковое значение.
 */
public final class PlateNormalizer {

    private PlateNormalizer() {
    }

    public static String normalize(String plate) {
        if (plate == null) {
            return "";
        }
        return plate.toUpperCase()
                .replace('A', 'А').replace('B', 'В').replace('E', 'Е')
                .replace('K', 'К').replace('M', 'М').replace('H', 'Н')
                .replace('O', 'О').replace('P', 'Р').replace('C', 'С')
                .replace('T', 'Т').replace('X', 'Х').replace('Y', 'У')
                .replaceAll("[\\s.\\-]", "");
    }
}
