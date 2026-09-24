# Первичные источники и границы переноса

Проверено 22.09.2026. Ссылки являются техническими источниками, а не обещанием доступности всех данных любому аккаунту.

**S1. GPTL — программный интерфейс.** https://s3.gptl.ru/geoportal-public/pro-guide/v1/index.html . STAC, OAuth-клиенты, refresh token, STS, временные S3-реквизиты, ограничения доступа. Реализация не использует чужой client_id из примеров.

**S2. WMO OSCAR — MSU-GS/A.** https://space.oscar.wmo.int/instruments/view/msu_gs_a . Номинальные центральные длины волн, спектральные интервалы, разрешение и назначение прибора для «Арктики-М». Для конкретного аппарата всё равно требуется его радиометрическая спецификация; запись OSCAR не заменяет коэффициенты продукции GPTL.

**S3. Rasterio — reprojection.** https://rasterio.readthedocs.io/en/stable/topics/reproject.html . Модель src/dst CRS/transform и ресэмплинг. Конкретный выбор ближайшего соседа в MVP — инженерное решение, а не универсальная рекомендация для всех метеопродуктов.

**S4. Satpy — generic enhancements.** https://raw.githubusercontent.com/pytroll/satpy/main/satpy/etc/enhancements/generic.yaml . Использованы формулы/диапазоны вариантов fog_default, dust_default, ash_default. Имплементация своя; перенос на МСУ-ГС/А не объявлен верифицированным.

**S5. EUMeTrain — Night Microphysics.** https://resources.eumetrain.org/data/4/410/print_4.htm . Рецепт, физическая интерпретация и ограничение ночными условиями. Маска −6° в коде — дополнительная экспериментальная настройка приложения.

**S6. NOAA GML — Solar calculation details.** https://gml.noaa.gov/grad/solcalc/solareqns.PDF . Приближённые уравнения положения Солнца. В приложении нет рефракции и времени каждого пикселя.

**S7. Stoll, 2022. A global climatology of polar lows investigated for local differences and wind-shear environments.** Weather and Climate Dynamics, 3, 483–504. https://wcd.copernicus.org/articles/3/483/2022/ . Определения, различия регионов и условий, ограничения простых критериев. Порог из статьи не превращён в универсальную вероятность возникновения ПМЦ.

**S8. Natural Earth — terms of use.** https://www.naturalearthdata.com/about/terms-of-use/ . Общественное достояние подложки; грубая обзорная карта не является навигационной.
