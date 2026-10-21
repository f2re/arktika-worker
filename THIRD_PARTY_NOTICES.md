# Сторонние компоненты и права на данные

## 1. Картографическая подложка

Файл `static/land.geojson` сформирован на основе открытого набора геоданных Natural Earth низкого разрешения (`naturalearth_lowres`), поставляемого в тестовых наборах `pyogrio`. Границы государств объединены в полигональную геометрию суши для отображения береговой линии на карте.

Natural Earth относит свои картографические данные к общественному достоянию (Public Domain): https://www.naturalearthdata.com/about/terms-of-use/. Данная векторная геометрия служит исключительно для визуальной ориентировки оператора и не предназначена для судовождения, точного демаркирования государственных границ или детального анализа ледовой кромки.

Библиотеки `pyogrio`, `Shapely` и `GeoPandas` для штатной работы приложения не требуются: готовый файл геометрии встроен в репозиторий.

---

## 2. Программные библиотеки и зависимости

Исходный код приложения не содержит бинарных файлов сторонних библиотек. Все используемые библиотеки устанавливаются стандартным пакетным менеджером `pip` из PyPI и сохраняют условия своих лицензий:

### 2.1. Базовый георастровый стек:
- **NumPy** — BSD-3-Clause License (Copyright © NumPy Developers)
- **Rasterio** — BSD-3-Clause License (Copyright © Mapbox)
- **pyproj** — MIT License (Copyright © pyproj contributors)
- **Pillow** — HPND License (Historical Permission Notice and Disclaimer, Copyright © Jeffrey A. Clark and contributors)

### 2.2. Модуль пространственных полей реанализа и орбитальной геометрии (0.3.0):
- **sgp4** — MIT License (Copyright © Brandon Rhodes) — расчёт положения КА и углов визирования по модели SGP4/TLE.
- **xarray** — Apache License 2.0 (Copyright © xarray Developers) — многомерные метеорологические массивы данных.
- **netCDF4** — MIT License (Copyright © Jeffrey Whitaker) — интерфейс к бинарным файлам NetCDF4/HDF5.
- **SciPy** — BSD-3-Clause License (Copyright © SciPy Developers) — пространственная интерполяция сеток.
- **contourpy** — BSD-3-Clause License (Copyright © ContourPy Developers) — расчёт изолиний метеорологических величин.
- **pydap** — BSD-3-Clause License (Copyright © Roberto De Almeida) — OPeNDAP-клиент для доступа к MERRA-2.
- **requests** — Apache License 2.0 (Copyright © Kenneth Reitz and contributors) — HTTP-транспорт для загрузки данных.
- **cdsapi** — Apache License 2.0 (Copyright © ECMWF) — клиент доступа к архиву Copernicus Climate Data Store.

---

## 3. Права на внешние метеорологические данные

### 3.1. Спутниковые данные космической системы «Арктика-М»
Снимки прибора МСУ-ГС, файлы L1B/L2IR, каталожные метаданные STAC и изображения, получаемые через геопортал GPTL (НЦ ОМЗ / Госкорпорация «Роскосмос»), не становятся общественным достоянием или объектами лицензии MIT при обработке в данном приложении. Условия использования, публикации и распространения спутниковых данных определяются оператором космической системы. Исходные спутниковые GeoTIFF пользователя не входят в открытый репозиторий проекта.

### 3.2. Реанализы ERA5 и CARRA2 (Copernicus Climate Change Service / ECMWF)
Поля глобального реанализа ERA5 и регионального панарктического реанализа CARRA2 предоставляются Европейским центром среднесрочных прогнозов погоды (ECMWF) в рамках программы Copernicus. Данные доступны на условиях открытой лицензии Copernicus License: https://cds.climate.copernicus.eu/. Пользователь обязан самостоятельно ознакомиться и принять лицензионные условия в интерфейсе Copernicus CDS перед использованием токена.

### 3.3. Реанализ MERRA-2 (NASA Global Modeling and Assimilation Office)
Поля реанализа MERRA-2 предоставляются NASA GES DISC в соответствии с политикой открытых данных NASA Earth Science Data Policy: https://www.earthdata.nasa.gov/engage/open-data-services-and-software/data-and-information-policy. Доступ требует бесплатной регистрации учетной записи NASA Earthdata Login.

---

## 4. Спектральные функции и радиационные модели

### 4.1. Спектральные характеристики МСУ-ГС
Файл `arktika/electrol2_srf.json` содержит опубликованные численные таблицы спектральной чувствительности каналов 4–10 прибора МСУ-ГС КА «Электро-Л» №2, предоставленные NWP SAF (составитель Andrei Filei, НИЦ «Планета», ноябрь 2019): https://nwp-saf.eumetsat.int/. Таблицы используются в качестве физического аналога для исследовательских расчётов пропускания атмосферы.

### 4.2. Модель RTTOV
Радиационная модель RTTOV (Radiative Transfer for TOVS) разработана консорциумом NWP SAF (EUMETSAT / Met Office / Météo-France / ECMWF). Программный пакет RTTOV не входит в состав репозитория. Пользователь загружает и компилирует RTTOV самостоятельно после регистрации на сайте NWP SAF: https://nwp-saf.eumetsat.int/.

---

## 5. Методики цветосинтеза (RGB)

Математические диапазоны и формулы композиций RGB сопоставлены с открытыми спецификациями EUMETSAT и EUMeTrain (24-hour Microphysics, Night Microphysics, Dust RGB, Ash RGB), а также рецептами библиотеки Satpy. Программная реализация алгоритмов выполнена самостоятельно в коде проекта. Использованные справочные материалы подробно задокументированы в `docs/SCIENCE.md` и `docs/SOURCES.md`.
