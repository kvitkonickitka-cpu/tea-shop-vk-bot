# Сканер для страницы сборки

`zxing-reader.js` и `zxing_reader.wasm` — сборка
[zxing-wasm](https://github.com/Sec-ant/zxing-wasm) 3.1.4 (MIT, лицензия в
`zxing-wasm.LICENSE`), внутри — zxing-cpp (Apache-2.0). Взяты из
npm-пакета: `dist/iife/reader/index.js` и `dist/reader/zxing_reader.wasm`.

Лежат в репозитории, а не грузятся с CDN: страница открывается на телефоне
сборщика, и её работа не должна зависеть от того, доступен ли jsDelivr.

Проверено, что с `textMode: "Plain"` DataMatrix стандарта GS1 читается с
разделителем GS (0x1D) внутри и идентификатором символики `]d2` — без этого
код маркировки для кассы непригоден.
